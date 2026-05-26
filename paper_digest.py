import os
import re
import smtplib
import textwrap
import requests
import yaml
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import argostranslate.package
import argostranslate.translate

CONFIG_FILE = "config.yml"
SEEN_FILE = "seen_pmids.txt"


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = load_config()

JOURNALS = CONFIG["journals"]

KEYWORD_GROUPS = CONFIG["keyword_groups"]

KEYWORDS_HIGH = KEYWORD_GROUPS["high"]["terms"]
KEYWORDS_DISEASE = KEYWORD_GROUPS["disease"]["terms"]
KEYWORDS_EXTRA = KEYWORD_GROUPS["extra"]["terms"]


def load_seen_pmids():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_seen_pmids(pmids):
    old = load_seen_pmids()
    merged = sorted(old.union(set(pmids)))
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for pmid in merged:
            f.write(pmid + "\n")


def build_pubmed_query(days=3):
    # 毎日実行でも、PubMed側の登録遅延を考えて直近3日を見る
    journal_query = " OR ".join([f'"{j}"[Journal]' for j in JOURNALS])

    topic_terms = KEYWORDS_HIGH + KEYWORDS_DISEASE + KEYWORDS_EXTRA
    topic_query = " OR ".join([f'"{k}"[Title/Abstract]' for k in topic_terms])

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    date_query = f'("{start}"[Date - Entrez] : "{today}"[Date - Entrez])'

    query = f"({journal_query}) AND ({topic_query}) AND {date_query}"
    return query


def pubmed_esearch(query, retmax=50):
    email = os.environ.get("NCBI_EMAIL", "")
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": retmax,
        "sort": "pub+date",
        "email": email,
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("esearchresult", {}).get("idlist", [])

def get_text_or_empty(parent, path):
    el = parent.find(path)
    return el.text.strip() if el is not None and el.text else ""


def extract_publication_date(article):
    # ArticleDate があれば優先する
    article_date = article.find(".//ArticleDate")
    if article_date is not None:
        year = get_text_or_empty(article_date, "Year")
        month = get_text_or_empty(article_date, "Month")
        day = get_text_or_empty(article_date, "Day")

        if year:
            parts = [year]
            if month:
                parts.append(month.zfill(2))
            if day:
                parts.append(day.zfill(2))
            return "-".join(parts)

    # JournalIssue の PubDate を使う
    pub_date = article.find(".//Journal/JournalIssue/PubDate")
    if pub_date is not None:
        year = get_text_or_empty(pub_date, "Year")
        month = get_text_or_empty(pub_date, "Month")
        day = get_text_or_empty(pub_date, "Day")
        medline_date = get_text_or_empty(pub_date, "MedlineDate")

        if year:
            parts = [year]
            if month:
                parts.append(month)
            if day:
                parts.append(day)
            return " ".join(parts)

        if medline_date:
            return medline_date

    return "Unknown date"


def extract_pubmed_entry_date(article):
    # PubMedに登録された日付を取る
    for status in ["pubmed", "entrez", "medline"]:
        date_el = article.find(f".//PubmedData/History/PubMedPubDate[@PubStatus='{status}']")
        if date_el is not None:
            year = get_text_or_empty(date_el, "Year")
            month = get_text_or_empty(date_el, "Month")
            day = get_text_or_empty(date_el, "Day")

            if year and month and day:
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    return "Unknown date"

def pubmed_efetch(pmids):
    if not pmids:
        return []

    email = os.environ.get("NCBI_EMAIL", "")
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "email": email,
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    articles = []

    for article in root.findall(".//PubmedArticle"):
        pmid_el = article.find(".//PMID")
        pmid = pmid_el.text if pmid_el is not None else ""

        title_el = article.find(".//ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else "No title"

        journal_el = article.find(".//Journal/Title")
        journal = journal_el.text.strip() if journal_el is not None and journal_el.text else "Unknown journal"

        pub_date = extract_publication_date(article)
        pubmed_entry_date = extract_pubmed_entry_date(article)

        abstract_parts = []
        for abs_el in article.findall(".//Abstract/AbstractText"):
            label = abs_el.attrib.get("Label")
            text = " ".join("".join(abs_el.itertext()).split())
            if label:
                abstract_parts.append(f"{label}: {text}")
            else:
                abstract_parts.append(text)

        abstract = "\n".join(abstract_parts).strip()

        articles.append(
            {
                "pmid": pmid,
                "title": title,
                "journal": journal,
                "pub_date": pub_date,
                "pubmed_entry_date": pubmed_entry_date,
                "abstract": abstract,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
            }
        )

    return articles


def normalize_text(text):
    return re.sub(r"\s+", " ", text.lower())


def contains_term(text, term):
    text = text.lower()
    term_lower = term.lower()

    # PRS, GWAS, ADHD など短い略語は単語境界で判定する
    if len(term_lower) <= 5 and re.fullmatch(r"[a-z0-9]+", term_lower):
        return re.search(rf"\b{re.escape(term_lower)}\b", text) is not None

    return term_lower in text


def score_article(article):
    text = normalize_text(
        article["title"] + " " + article["abstract"] + " " + article["journal"]
    )

    score = 0
    reasons = []

    journal_scores = {
        journal.lower(): value
        for journal, value in CONFIG.get("journal_scores", {}).items()
    }

    journal_name = article["journal"].lower()

    if journal_name in journal_scores:
        add_score = journal_scores[journal_name]
        score += add_score
        reasons.append(f"主要誌 +{add_score}")

    for group_name, group in CONFIG["keyword_groups"].items():
        group_score = group["score"]
        terms = group["terms"]

        for term in terms:
            if contains_term(text, term):
                score += group_score
                reasons.append(f"{term} +{group_score}")

    reasons = list(dict.fromkeys(reasons))

    return score, reasons


def setup_argos_translation():
    installed_languages = argostranslate.translate.get_installed_languages()
    has_en_ja = any(
        lang.code == "en" and any(t.to_lang.code == "ja" for t in lang.translations_from)
        for lang in installed_languages
    )

    if has_en_ja:
        return

    argostranslate.package.update_package_index()
    available_packages = argostranslate.package.get_available_packages()

    package_to_install = None
    for package in available_packages:
        if package.from_code == "en" and package.to_code == "ja":
            package_to_install = package
            break

    if package_to_install is None:
        raise RuntimeError("English to Japanese translation package was not found.")

    path = package_to_install.download()
    argostranslate.package.install_from_path(path)


def translate_to_japanese(text):
    if not text:
        return "Abstractなし"

    # 長すぎる場合に備えて分割
    chunks = textwrap.wrap(text, width=1800, break_long_words=False, replace_whitespace=False)
    translated_chunks = []

    for chunk in chunks:
        translated = argostranslate.translate.translate(chunk, "en", "ja")
        translated_chunks.append(translated)

    return "\n".join(translated_chunks)


def build_email_body(articles):
    today_jst = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")

    if not articles:
        return f"本日の該当論文はありませんでした。\n\nDate: {today_jst}"

    lines = []
    lines.append(f"精神医学・遺伝統計学 注目論文 Daily Digest")
    lines.append(f"Date: {today_jst}")
    lines.append("")
    lines.append(f"本日の抽出論文数: {len(articles)}")
    lines.append("")

    for i, a in enumerate(articles, start=1):
        reasons = ", ".join(a["reasons"]) if a["reasons"] else "主要キーワードに該当"

        lines.append("=" * 80)
        lines.append(f"{i}. {a['title']}")
        lines.append("")
        lines.append(f"Journal: {a['journal']}")
        lines.append(f"Publication date: {a['pub_date']}")
        lines.append(f"PubMed entry date: {a.get('pubmed_entry_date', 'Unknown date')}")
        lines.append(f"PMID: {a['pmid']}")
        lines.append(f"Score: {a['score']}")
        lines.append(f"注目理由: {reasons}")
        lines.append(f"PubMed: {a['url']}")
        lines.append("")
        lines.append("Abstract 日本語訳:")
        lines.append(a["abstract_ja"])
        lines.append("")

        if a["abstract"]:
            lines.append("Original Abstract:")
            lines.append(a["abstract"])
            lines.append("")

    return "\n".join(lines)


def send_email(subject, body):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    mail_to = os.environ["MAIL_TO"]

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = mail_to
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.send_message(msg)


def main():
    seen_pmids = load_seen_pmids()

    query = build_pubmed_query(days=CONFIG.get("lookback_days", 3))
    pmids = pubmed_esearch(query, retmax=80)

    new_pmids = [pmid for pmid in pmids if pmid not in seen_pmids]

    if not new_pmids:
        print("No new articles.")
        return

    articles = pubmed_efetch(new_pmids)

    scored_articles = []
    for article in articles:
        score, reasons = score_article(article)
        article["score"] = score
        article["reasons"] = reasons

        # スコアが低すぎるものは送らない
        if score >= CONFIG.get("score_threshold", 5):
            scored_articles.append(article)

    scored_articles = sorted(scored_articles, key=lambda x: x["score"], reverse=True)

    # 毎日読む量として最大5本
    selected_articles = scored_articles[:CONFIG.get("max_articles", 5)]

    if not selected_articles:
        print("No articles passed score threshold.")
        return

    setup_argos_translation()

    for article in selected_articles:
        article["abstract_ja"] = translate_to_japanese(article["abstract"])

    today_jst = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    subject = f"Daily論文Digest 精神医学・遺伝統計 {today_jst}"
    body = build_email_body(selected_articles)

    send_email(subject, body)

    save_seen_pmids([a["pmid"] for a in selected_articles])

    print(f"Sent {len(selected_articles)} articles.")


if __name__ == "__main__":
    main()
