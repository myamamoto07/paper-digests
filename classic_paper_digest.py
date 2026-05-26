import os
import re
import smtplib
import requests
import yaml
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import xml.etree.ElementTree as ET
from urllib.parse import quote


CONFIG_FILE = "classic_config.yml"
SEEN_FILE = "seen_classic_ids.txt"

OPENALEX_WORKS_URL = "https://api.openalex.org/works"


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = load_config()


def load_seen_ids():
    if not os.path.exists(SEEN_FILE):
        return set()

    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_seen_ids(ids):
    old = load_seen_ids()
    merged = sorted(old.union(set(ids)))

    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for item in merged:
            f.write(item + "\n")


def get_today_theme():
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst)
    weekday = today.strftime("%A").lower()

    themes = CONFIG["weekday_themes"]
    return weekday, themes[weekday]


def reconstruct_abstract(abstract_inverted_index):
    """
    OpenAlexのabstract_inverted_indexを通常のAbstract文に戻す。
    abstractがない論文では空文字を返す。
    """
    if not abstract_inverted_index:
        return ""

    positions = []
    for word, idxs in abstract_inverted_index.items():
        for idx in idxs:
            positions.append((idx, word))

    if not positions:
        return ""

    positions.sort(key=lambda x: x[0])
    words = [word for _, word in positions]

    return " ".join(words)


def get_source_name(work):
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}

    if source.get("display_name"):
        return source["display_name"]

    locations = work.get("locations") or []
    for loc in locations:
        src = (loc or {}).get("source") or {}
        if src.get("display_name"):
            return src["display_name"]

    return "Unknown journal"


def get_work_url(work):
    if work.get("doi"):
        return work["doi"]

    if work.get("id"):
        return work["id"]

    return ""


def normalize_text(text):
    return re.sub(r"\s+", " ", (text or "").lower())


def has_excluded_term(title):
    title_l = normalize_text(title)
    for term in CONFIG.get("exclude_terms", []):
        if term.lower() in title_l:
            return True
    return False

def clean_doi(doi):
    if not doi:
        return ""
    doi = doi.strip()
    doi = doi.replace("https://doi.org/", "")
    doi = doi.replace("http://doi.org/", "")
    return doi


def fetch_pubmed_abstract_by_doi(doi):
    doi = clean_doi(doi)
    if not doi:
        return ""

    try:
        email = os.environ.get("NCBI_EMAIL", "")

        search_params = {
            "db": "pubmed",
            "term": f"{doi}[DOI]",
            "retmode": "json",
        }
        if email:
            search_params["email"] = email

        search_res = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params=search_params,
            timeout=20,
        )
        search_res.raise_for_status()

        ids = search_res.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return ""

        pmid = ids[0]

        fetch_params = {
            "db": "pubmed",
            "id": pmid,
            "retmode": "xml",
        }
        if email:
            fetch_params["email"] = email

        fetch_res = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params=fetch_params,
            timeout=20,
        )
        fetch_res.raise_for_status()

        root = ET.fromstring(fetch_res.text)
        abstract_parts = []

        for elem in root.findall(".//Abstract/AbstractText"):
            text = " ".join(elem.itertext()).strip()
            label = elem.attrib.get("Label")
            if label:
                abstract_parts.append(f"{label}: {text}")
            else:
                abstract_parts.append(text)

        return "\n".join([p for p in abstract_parts if p])

    except Exception as e:
        print(f"PubMed abstract fetch failed for DOI {doi}: {e}")
        return ""


def fetch_crossref_abstract_by_doi(doi):
    doi = clean_doi(doi)
    if not doi:
        return ""

    try:
        url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
        res = requests.get(url, timeout=20)
        res.raise_for_status()

        abstract = res.json().get("message", {}).get("abstract", "")
        if not abstract:
            return ""

        abstract = re.sub(r"<[^>]+>", " ", abstract)
        abstract = re.sub(r"\s+", " ", abstract).strip()
        return abstract

    except Exception as e:
        print(f"Crossref abstract fetch failed for DOI {doi}: {e}")
        return ""


def fill_missing_abstract(article):
    if article.get("abstract"):
        return article

    doi = article.get("doi", "")

    abstract = fetch_pubmed_abstract_by_doi(doi)

    if not abstract:
        abstract = fetch_crossref_abstract_by_doi(doi)

    if abstract:
        article["abstract"] = abstract

    return article

def work_to_article(work):
    title = work.get("display_name") or "No title"
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))

    article = {
        "id": work.get("id", ""),
        "title": title,
        "journal": get_source_name(work),
        "year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count") or 0,
        "doi": work.get("doi") or "",
        "url": get_work_url(work),
        "type": work.get("type") or "",
        "abstract": abstract,
        "authors": extract_authors(work),
    }

    return article


def extract_authors(work, max_authors=8):
    authorships = work.get("authorships") or []
    names = []

    for authorship in authorships[:max_authors]:
        author = authorship.get("author") or {}
        name = author.get("display_name")
        if name:
            names.append(name)

    if len(authorships) > max_authors:
        names.append("et al.")

    return ", ".join(names)


def passes_basic_filters(article):
    if not article["id"]:
        return False

    if has_excluded_term(article["title"]):
        return False

    year = article.get("year")
    if year is None:
        return False

    if year < CONFIG.get("min_publication_year", 1950):
        return False

    if year > CONFIG.get("max_publication_year", 2021):
        return False

    if article["cited_by_count"] < CONFIG.get("min_citations", 300):
        return False

    if article["cited_by_count"] > CONFIG.get("max_citations", 10000):
        return False

    text = normalize_text(article["title"] + " " + article["abstract"] + " " + article["journal"])

    required_terms = CONFIG.get("required_relevance_terms", [])
    if required_terms:
        if not any(term.lower() in text for term in required_terms):
            return False
    
    # 明らかな非論文タイプを避ける
    bad_types = {"editorial", "letter", "erratum", "paratext"}
    if article.get("type", "").lower() in bad_types:
        return False

    return True


def score_article(article, theme):
    """
    基本は引用数ベース。
    そこに雑誌ボーナスとテーマ語ボーナスを少し足す。
    """
    score = article["cited_by_count"]

    journal_bonus = CONFIG.get("journal_bonus", {})
    score += journal_bonus.get(article["journal"], 0)

    text = normalize_text(article["title"] + " " + article["abstract"])

    theme_bonus = 0
    for term in theme.get("query_terms", []):
        if term.lower() in text:
            theme_bonus += 30

    score += theme_bonus

    reasons = [
        f"引用数 {article['cited_by_count']}",
    ]

    if article["journal"] in journal_bonus:
        reasons.append(f"雑誌ボーナス +{journal_bonus[article['journal']]}")

    if theme_bonus:
        reasons.append(f"テーマ一致 +{theme_bonus}")

    return score, reasons


def search_openalex_for_term(term, per_page=20):
    api_key = os.environ.get("OPENALEX_API_KEY")
    if not api_key:
        raise RuntimeError("OPENALEX_API_KEY is not set.")

    params = {
        "search": term,
        "per-page": per_page,
        "sort": "cited_by_count:desc",
        "api_key": api_key,
    }

    # 連絡先メールがあれば一緒に渡す
    mailto = os.environ.get("NCBI_EMAIL")
    if mailto:
        params["mailto"] = mailto

    r = requests.get(OPENALEX_WORKS_URL, params=params, timeout=30)
    r.raise_for_status()

    data = r.json()
    return data.get("results", [])


def collect_candidates(theme):
    seen_ids = load_seen_ids()
    candidates = {}

    max_candidates = CONFIG.get("max_candidates", 50)
    per_term = 20

    for term in theme.get("query_terms", []):
        print(f"Searching OpenAlex: {term}")

        try:
            works = search_openalex_for_term(term, per_page=per_term)
        except Exception as e:
            print(f"OpenAlex search failed for term={term}: {e}")
            continue

        for work in works:
            article = work_to_article(work)

            if article["id"] in seen_ids:
                continue

            if not passes_basic_filters(article):
                continue

            score, reasons = score_article(article, theme)
            article["score"] = score
            article["reasons"] = reasons

            # 同じ論文が複数キーワードで出た場合は高いscoreを採用
            if article["id"] not in candidates:
                candidates[article["id"]] = article
            else:
                if article["score"] > candidates[article["id"]]["score"]:
                    candidates[article["id"]] = article

    articles = list(candidates.values())
    articles = sorted(articles, key=lambda x: x["score"], reverse=True)

    return articles[:max_candidates]


def translate_to_japanese(text):
    if not text:
        return "Abstractなし"

    gas_url = os.environ.get("GAS_TRANSLATE_URL")
    gas_token = os.environ.get("GAS_TRANSLATE_TOKEN")

    if not gas_url or not gas_token:
        raise RuntimeError("GAS_TRANSLATE_URL or GAS_TRANSLATE_TOKEN is not set.")

    payload = {
        "token": gas_token,
        "text": text,
        "source": "en",
        "target": "ja",
    }

    r = requests.post(gas_url, json=payload, timeout=60)
    r.raise_for_status()

    data = r.json()

    if not data.get("ok"):
        raise RuntimeError(f"Google Apps Script translation failed: {data.get('error')}")

    return data.get("translated", "")


def build_email_body(articles, theme_label):
    today_jst = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")

    lines = []
    lines.append("Classic Paper Digest")
    lines.append(f"Date: {today_jst}")
    lines.append(f"本日のテーマ: {theme_label}")
    lines.append(f"紹介論文数: {len(articles)}")
    lines.append("")

    for i, article in enumerate(articles, start=1):
        lines.append("=" * 80)
        lines.append("")
        lines.append(f"{i}. {article['title']}")
        lines.append("")
        lines.append(f"Authors: {article['authors']}")
        lines.append(f"Journal: {article['journal']}")
        lines.append(f"Publication year: {article['year']}")
        lines.append(f"Citation count: {article['cited_by_count']}")
        lines.append(f"OpenAlex score: {article['score']}")
        lines.append(f"Type: {article['type']}")
        lines.append(f"DOI: {article['doi']}")
        lines.append(f"URL: {article['url']}")
        lines.append("")
        lines.append("選定理由:")
        for reason in article["reasons"]:
            lines.append(f"- {reason}")
        lines.append("")
        lines.append("なぜ読むべきか:")
        lines.append(make_reading_note(article, theme_label))
        lines.append("")
        lines.append("Abstract 日本語訳（機械翻訳・要確認）:")
        lines.append(article.get("abstract_ja", "Abstractなし"))
        lines.append("")
        lines.append("Original Abstract:")
        lines.append(article.get("abstract", "Abstractなし"))
        lines.append("")

    return "\n".join(lines)

def make_reading_note(article, theme_label):
    notes = []

    notes.append(
        f"この論文は「{theme_label}」の文脈で高く引用されているため、分野の基盤知識として読む価値があります。"
    )

    if article["cited_by_count"] >= 5000:
        notes.append("引用数が非常に多く、分野横断的に強い影響を持った可能性があります。")
    elif article["cited_by_count"] >= 1000:
        notes.append("引用数が多く、後続研究の前提になっている可能性があります。")
    else:
        notes.append("古典・基盤論文として一定以上の影響を持つ候補です。")

    title_l = article["title"].lower()

    if "polygenic" in title_l or "gwas" in title_l or "genome-wide" in title_l:
        notes.append("GWAS・PRS・多遺伝子性の考え方を理解するうえで重要な可能性があります。")

    if "schizophrenia" in title_l or "bipolar" in title_l or "depression" in title_l:
        notes.append("主要精神疾患の病態・遺伝構造・臨床像を考えるうえで参考になります。")

    if "suicide" in title_l or "risk prediction" in title_l or "electronic health" in title_l:
        notes.append("自殺リスク予測や臨床データ活用の文脈で確認する価値があります。")

    return "\n".join(notes)


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
    weekday, theme = get_today_theme()
    theme_label = theme["label"]

    print(f"Today's theme: {weekday} / {theme_label}")

    candidates = collect_candidates(theme)

    if not candidates:
        print("No classic paper candidates found.")
        return

    max_articles = CONFIG.get("max_articles", 1)
    selected_articles = candidates[:max_articles]

    for article in selected_articles:
        article = fill_missing_abstract(article)

        if article.get("abstract"):
            article["abstract_ja"] = translate_to_japanese(article["abstract"])
        else:
            article["abstract_ja"] = "Abstractなし"

    today_jst = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    subject = f"Classic論文Digest {theme_label} {today_jst}"
    body = build_email_body(selected_articles, theme_label)

    send_email(subject, body)

    save_seen_ids([a["id"] for a in selected_articles])

    print(f"Sent {len(selected_articles)} classic papers.")
    for article in selected_articles:
        print(f"- {article['title']}")
        print(f"  OpenAlex ID: {article['id']}")
        print(f"  Citations: {article['cited_by_count']}")


if __name__ == "__main__":
    main()
