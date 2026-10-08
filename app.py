import os
from flask import Flask
from html.parser import HTMLParser
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
from supabase import create_client, Client

# ============================================================
# FLASK WEB SERVER SETUP (For Render & UptimeRobot)
# ============================================================
app = Flask(__name__)

@app.route('/')
def health_check():
    # UptimeRobot will ping this endpoint to keep the server awake
    return "Xylem Checker Worker is running continuously!", 200


# ============================================================
# SUPABASE CONFIGURATION
# ============================================================

SUPABASE_URL = "https://psfxcihqsoouetblpyvq.supabase.co"
SUPABASE_KEY = "sb_publishable_3MPpwBaNrhaJ5fVfiTE0eQ_2a6_NT8x"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
TABLE_NAME = "Xylem_database"


# ============================================================
# HTML TEXT EXTRACTION & PARSING HELPERS
# ============================================================

class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result = []
        self.ignore_tags = {"script", "style", "head", "title", "meta"}
        self.current_tag = ""

    def handle_starttag(self, tag, attrs):
        self.current_tag = tag.lower()

    def handle_data(self, data):
        if self.current_tag not in self.ignore_tags:
            text = data.strip()
            if text:
                self.result.append(text)

    def get_text(self):
        return "\n".join(self.result)


def extract_plain_text(html_content):
    parser = HTMLTextExtractor()
    parser.feed(html_content)
    text = parser.get_text()
    return re.sub(r"\n\s*\n+", "\n", text)


def parse_student_info(clean_text, default_roll):
    """
    Extracts Name, Roll Number, Score, and Rank.
    """
    # Name extraction
    name_match = re.search(r"(?:Name|Student Name)\s*[:\-]?\s*(.+)", clean_text, re.IGNORECASE)
    name = name_match.group(1).strip() if name_match else "N/A"

    # Roll Number extraction (int8/bigint)
    roll_match = re.search(r"(?:Roll\s*(?:No|Number)?)\s*[:\-]?\s*(\d+)", clean_text, re.IGNORECASE)
    try:
        roll = int(roll_match.group(1).strip()) if roll_match else int(default_roll)
    except ValueError:
        roll = int(default_roll)

    # Score / Marks extraction (int2)
    score_match = re.search(r"(?:Score|Total\s*Marks?|Marks)\s*[:\-]?\s*([\d\.]+)", clean_text, re.IGNORECASE)
    try:
        score = int(float(score_match.group(1).strip())) if score_match else None
    except ValueError:
        score = None

    # Rank extraction (int2)
    rank_match = re.search(r"(?:Rank|State\s*Rank|AIR)\s*[:\-]?\s*(\d+)", clean_text, re.IGNORECASE)
    try:
        rank = int(rank_match.group(1).strip()) if rank_match else None
    except ValueError:
        rank = None

    return {
        "name": name,
        "number": roll,
        "score": score,
        "rank": rank
    }


# ============================================================
# CONFIGURATION
# ============================================================

API_URL = "https://results.xylemlearning.com/wp-admin/admin-ajax.php"

EXAM_CLASS = "JEE"
EXAM_YEAR = "GIB-27SEP2026"

START_ROLL = 9160000000
END_ROLL = 9169999999

WORKERS = 30
BATCH_SIZE = 1000
REQUEST_TIMEOUT = 100

# ============================================================
# THREAD-LOCAL HTTP SESSION
# ============================================================

thread_local = threading.local()


def get_session():
    if not hasattr(thread_local, "session"):
        session = requests.Session()
        session.headers.update({
            "User-Agent": "AuthorizedResultClient/1.0",
            "Accept": "text/html,application/json,*/*",
        })
        thread_local.session = session
    return thread_local.session


# ============================================================
# CHECK ONE ROLL NUMBER
# ============================================================

def check_roll(roll_number):
    session = get_session()

    data = {
        "action": "jsrmsp_student_result_view",
        "examclass": EXAM_CLASS,
        "examyear": EXAM_YEAR,
        "examroll": str(roll_number),
    }

    try:
        response = session.post(
            API_URL,
            data=data,
            timeout=REQUEST_TIMEOUT
        )

        status = response.status_code
        text = response.text.strip()

        if status != 200:
            return {
                "roll": roll_number,
                "status": "error",
                "message": f"HTTP {status}"
            }

        if not text:
            return {
                "roll": roll_number,
                "status": "error",
                "message": "Empty response"
            }

        if (
                "Checking your browser before redirecting" in text
                or "anubis_challenge" in text
                or "/.within.website/" in text
        ):
            return {
                "roll": roll_number,
                "status": "blocked",
                "message": "Backend request was blocked by Anubis"
            }

        if "Result not found or not published yet" in text:
            return {
                "roll": roll_number,
                "status": "not_found",
                "message": None
            }

        # RESULT FOUND - Extract and parse specific details
        clean_text = extract_plain_text(text)
        extracted_data = parse_student_info(clean_text, roll_number)

        return {
            "roll": roll_number,
            "status": "found",
            "message": None,
            "data": extracted_data
        }

    except requests.Timeout:
        return {
            "roll": roll_number,
            "status": "error",
            "message": "Request timeout"
        }

    except requests.RequestException as e:
        return {
            "roll": roll_number,
            "status": "error",
            "message": str(e)
        }


# ============================================================
# MAIN CHECKING LOGIC
# ============================================================

def main():
    total = END_ROLL - START_ROLL

    print("=" * 70)
    print("BACKEND RESULT CHECKER (SUPABASE DB INTEGRATION)")
    print("=" * 70)

    print(f"Start roll : {START_ROLL}")
    print(f"End roll   : {END_ROLL - 1}")
    print(f"Total      : {total:,}")
    print(f"Workers    : {WORKERS}")
    print(f"Batch size : {BATCH_SIZE}")
    print()

    start_time = time.time()

    checked = 0
    found = 0
    not_found = 0
    errors = 0
    blocked = 0

    for batch_start in range(START_ROLL, END_ROLL, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, END_ROLL)

        print(f"\nBatch: {batch_start} - {batch_end - 1}", flush=True)

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [
                executor.submit(check_roll, roll)
                for roll in range(batch_start, batch_end)
            ]

            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as e:
                    checked += 1
                    errors += 1
                    print(f"[ERROR] {e}", flush=True)
                    continue

                checked += 1
                roll = result["roll"]
                status = result["status"]

                if status == "found":
                    found += 1
                    info = result["data"]

                    # Insert directly into Supabase database table
                    try:
                        res = supabase.table(TABLE_NAME).insert({
                            "name": info["name"],
                            "number": info["number"],
                            "score": info["score"],
                            "rank": info["rank"]
                        }).execute()

                        print(
                            f"[FOUND & INSERTED] {roll} | {checked:,}/{total:,}",
                            flush=True
                        )
                    except Exception as db_err:
                        print(
                            f"[DB INSERT ERROR] Roll: {roll} | Details: {db_err}",
                            flush=True
                        )

                elif status == "not_found":
                    not_found += 1
                    print(
                        f"[NOT FOUND] {roll} | {checked:,}/{total:,}",
                        flush=True
                    )

                elif status == "blocked":
                    blocked += 1
                    print(
                        f"[BLOCKED]   {roll} | Anubis/anti-bot protection",
                        flush=True
                    )

                else:
                    errors += 1
                    print(
                        f"[ERROR]     {roll} | {result['message']}",
                        flush=True
                    )

        print(
            f"\nProgress: {checked:,}/{total:,} | "
            f"Found: {found:,} | "
            f"Not found: {not_found:,} | "
            f"Errors: {errors:,} | "
            f"Blocked: {blocked:,}",
            flush=True
        )

    elapsed = time.time() - start_time

    print()
    print("=" * 70)
    print("FINISHED CURRENT ITERATION")
    print("=" * 70)

    print(f"Checked   : {checked:,}")
    print(f"Found     : {found:,}")
    print(f"Not found : {not_found:,}")
    print(f"Errors    : {errors:,}")
    print(f"Blocked   : {blocked:,}")
    print(f"Time      : {elapsed:.2f} seconds")
    print("=" * 70)


# ============================================================
# CONTINUOUS EXECUTION WRAPPER
# ============================================================

def run_continuous_loop():
    """Runs the main scraping logic in an infinite loop for Render deployment."""
    while True:
        try:
            print("\nStarting new continuous check iteration...")
            main()
        except Exception as e:
            print(f"Error in continuous execution loop: {e}")

        # Pause for 10 minutes (600 seconds) before starting the entire range again.
        # This prevents spamming the API continuously without taking a breath.
        print("\nIteration complete. Sleeping for 10 minutes before the next run...")
        time.sleep(600)


# ============================================================
# ENTRY POINT & WORKER STARTUP
# ============================================================

# Start the background thread when Gunicorn imports the file
worker_thread = threading.Thread(target=run_continuous_loop, daemon=True)
worker_thread.start()

if __name__ == "__main__":
    # Local testing fallback
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
