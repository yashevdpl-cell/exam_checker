"""
MCQ Exam Taker - Flask Application (Vercel & Local Ready)
"""
import os
import re
import json
import random
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from itsdangerous import URLSafeTimedSerializer

# ─── App Setup & Absolute Paths (Works seamlessly on Vercel & Local) ──────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
QUESTIONS_FILE = os.path.join(DATA_DIR, "questions.json")

# On serverless (Vercel), only /tmp is writable
IS_VERCEL = bool(os.environ.get("VERCEL"))
TMP_DIR = "/tmp" if IS_VERCEL else os.path.join(DATA_DIR, "tmp")
HISTORY_FILE = os.path.join(TMP_DIR, "history.json") if IS_VERCEL else os.path.join(DATA_DIR, "history.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.secret_key = os.environ.get("SECRET_KEY", "mcq_exam_secret_2024_secure_key")

# Cryptographic serializer for stateless exam sessions (zero-disk, serverless proof)
serializer = URLSafeTimedSerializer(app.secret_key)


# ─── Questions Storage & Cache ────────────────────────────────────────────────
_questions_cache = None

def load_questions():
    global _questions_cache
    if _questions_cache is not None:
        return _questions_cache
    if os.path.exists(QUESTIONS_FILE):
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                _questions_cache = json.load(f)
                return _questions_cache
        except Exception:
            return []
    return []

def get_question_dict():
    """Returns {id: question_obj} for fast O(1) lookup."""
    return {q["id"]: q for q in load_questions()}

def save_questions(questions):
    global _questions_cache
    _questions_cache = questions
    # On Vercel, write to /tmp or try writing to QUESTIONS_FILE
    target = os.path.join(TMP_DIR, "questions.json") if IS_VERCEL else QUESTIONS_FILE
    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(questions, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ─── PDF Parsing ──────────────────────────────────────────────────────────────
def parse_pdf(pdf_path):
    """Extract MCQ questions from CertyIQ style PDF."""
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                full_text += t + "\n"

    return parse_text(full_text)


def parse_text(text):
    """Parse MCQ text in the CertyIQ format into a list of question dicts."""
    blocks = re.split(r"(?=Question:\s*\d+\s)", text)
    questions = []
    seen = set()

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        q_num_match = re.match(r"Question:\s*(\d+)", block)
        if not q_num_match:
            continue
        q_num = int(q_num_match.group(1))
        if q_num in seen:
            continue
        seen.add(q_num)

        block = re.sub(r"Question:\s*\d+\s*(CertyIQ)?\s*\n?", "", block, count=1)

        answer_split = re.split(r"\nAnswer:\s*([A-E])\s*\n?", block, maxsplit=1)
        if len(answer_split) < 3:
            answer_split = re.split(r"Answer:\s*([A-E])\s*\n?", block, maxsplit=1)
        if len(answer_split) < 3:
            continue

        q_and_options = answer_split[0].strip()
        correct_letter = answer_split[1].strip().upper()
        explanation_text = answer_split[2].strip() if len(answer_split) > 2 else ""
        explanation_text = re.sub(r"^Explanation:\s*", "", explanation_text).strip()

        option_pattern = re.compile(r"\n([A-E])[.)]\s+(.+?)(?=\n[A-E][.)]\s|\Z)", re.DOTALL)
        option_matches = list(option_pattern.finditer("\n" + q_and_options))

        if not option_matches:
            continue

        first_opt_start = option_matches[0].start() - 1
        question_text = q_and_options[:first_opt_start].strip()
        question_text = re.sub(r"\s*CertyIQ\s*$", "", question_text).strip()

        options = {}
        for m in option_matches:
            letter = m.group(1).upper()
            opt_text = m.group(2).replace("\n", " ").strip()
            opt_text = re.sub(r"\s*CertyIQ\s*$", "", opt_text).strip()
            options[letter] = opt_text

        if not question_text or correct_letter not in options:
            continue

        questions.append({
            "id": str(q_num),
            "question": question_text,
            "options": options,
            "answer": correct_letter,
            "explanation": explanation_text[:1500] if explanation_text else ""
        })

    questions.sort(key=lambda x: int(x["id"]))
    return questions


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    questions = load_questions()
    return render_template("index.html", question_count=len(questions))


@app.route("/upload", methods=["POST"])
def upload_pdf():
    if "pdf" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["pdf"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    tmp_path = os.path.join(TMP_DIR, "uploaded.pdf")
    f.save(tmp_path)

    try:
        questions = parse_pdf(tmp_path)
    except Exception as e:
        return jsonify({"error": f"Failed to parse PDF: {str(e)}"}), 500

    if not questions:
        return jsonify({"error": "No questions found in this PDF"}), 400

    save_questions(questions)
    return jsonify({"count": len(questions), "message": f"Successfully extracted {len(questions)} questions!"})


@app.route("/load_default", methods=["POST"])
def load_default():
    """Load bundled questions from questions.json or local PDF."""
    questions = load_questions()
    if questions:
        return jsonify({"count": len(questions), "message": f"Loaded {len(questions)} Claude Certified Architect questions!"})

    default_pdf = r"c:\Users\Yash-Nanda\Downloads\claude-certified-architect.pdf"
    if os.path.exists(default_pdf):
        try:
            questions = parse_pdf(default_pdf)
            if questions:
                save_questions(questions)
                return jsonify({"count": len(questions), "message": f"Loaded {len(questions)} questions from PDF!"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "No bundled questions found. Please upload a PDF."}), 404


@app.route("/questions/count")
def questions_count():
    return jsonify({"count": len(load_questions())})


@app.route("/start_exam", methods=["POST"])
def start_exam():
    data = request.get_json() or {}
    num_questions = int(data.get("num_questions", 20))
    randomize = bool(data.get("randomize", True))
    incorrect_only = bool(data.get("incorrect_only", False))
    client_incorrect_ids = set(data.get("incorrect_ids", []))

    all_questions = [dict(q) for q in load_questions()]
    if not all_questions:
        return jsonify({"error": "No questions loaded. Please load questions first."}), 400

    # Filter incorrect questions if requested
    if incorrect_only:
        incorrect_ids = client_incorrect_ids
        if not incorrect_ids:
            for exam in load_history():
                for r in exam.get("results", []):
                    if not r.get("correct"):
                        incorrect_ids.add(str(r.get("id")))
        if incorrect_ids:
            all_questions = [q for q in all_questions if str(q["id"]) in incorrect_ids]
        if not all_questions:
            return jsonify({"error": "No incorrect questions found in history."}), 400

    if randomize:
        random.shuffle(all_questions)

    num_questions = min(num_questions, len(all_questions))
    selected = all_questions[:num_questions]

    # Map options per question
    for q in selected:
        opts = list(q["options"].items())
        if randomize:
            random.shuffle(opts)
        new_letters = "ABCDE"
        new_options = {}
        correct_text = q["options"][q["answer"]]
        for i, (_, text) in enumerate(opts):
            new_options[new_letters[i]] = text
        q["options"] = new_options
        for letter, text in new_options.items():
            if text == correct_text:
                q["answer"] = letter
                break

    # Stateless token encoding correct answers (tamper-proof, serverless-ready)
    answers_map = {str(q["id"]): q["answer"] for q in selected}
    exam_token = serializer.dumps({
        "answers": answers_map,
        "started_at": datetime.now().isoformat()
    })

    # Frontend receives question statement and options only
    frontend_questions = [{
        "id": str(q["id"]),
        "question": q["question"],
        "options": q["options"]
    } for q in selected]

    return jsonify({
        "exam_token": exam_token,
        "questions": frontend_questions,
        "total": len(frontend_questions)
    })


@app.route("/check_answer", methods=["POST"])
def check_answer():
    """Verify answer and return explanation from question bank."""
    data = request.get_json() or {}
    exam_token = data.get("exam_token")
    qid = str(data.get("question_id", ""))
    user_letter = str(data.get("user_answer", "")).upper()

    if not exam_token:
        return jsonify({"error": "Missing exam token."}), 400

    try:
        token_data = serializer.loads(exam_token, max_age=86400 * 3)  # Valid for 3 days
    except Exception:
        return jsonify({"error": "Exam session expired. Please start a new exam."}), 400

    correct_letter = token_data.get("answers", {}).get(qid, "")
    q_dict = get_question_dict()
    q_obj = q_dict.get(qid, {})
    explanation = q_obj.get("explanation", "")

    is_correct = (user_letter == correct_letter) if user_letter else False

    return jsonify({
        "correct_answer": correct_letter,
        "user_answer": user_letter,
        "is_correct": is_correct,
        "explanation": explanation
    })


@app.route("/submit_exam", methods=["POST"])
def submit_exam():
    data = request.get_json() or {}
    exam_token = data.get("exam_token")
    user_answers = data.get("answers", {})
    time_taken = data.get("time_taken", 0)

    if not exam_token:
        return jsonify({"error": "Missing exam token."}), 400

    try:
        token_data = serializer.loads(exam_token, max_age=86400 * 3)
    except Exception:
        return jsonify({"error": "Exam session expired or invalid."}), 400

    correct_answers = token_data.get("answers", {})
    q_dict = get_question_dict()

    results = []
    correct_count = 0
    incorrect_count = 0
    unattempted = 0

    for qid, correct_letter in correct_answers.items():
        user_letter = user_answers.get(qid, "")
        q_obj = q_dict.get(qid, {})
        explanation = q_obj.get("explanation", "")

        if not user_letter:
            unattempted += 1
            status = "unattempted"
            is_correct = False
        elif user_letter == correct_letter:
            correct_count += 1
            status = "correct"
            is_correct = True
        else:
            incorrect_count += 1
            status = "incorrect"
            is_correct = False

        results.append({
            "id": qid,
            "correct_answer": correct_letter,
            "user_answer": user_letter,
            "status": status,
            "correct": is_correct,
            "explanation": explanation
        })

    total = len(correct_answers)
    percentage = round((correct_count / total) * 100, 1) if total > 0 else 0

    exam_result = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": total,
        "correct": correct_count,
        "incorrect": incorrect_count,
        "unattempted": unattempted,
        "percentage": percentage,
        "time_taken": time_taken,
        "results": results
    }

    # Also persist to server history if storage permits
    history = load_history()
    history.append(exam_result)
    save_history(history)

    return jsonify(exam_result)


@app.route("/history")
def get_history():
    history = load_history()
    summary = [{
        "date": h["date"],
        "total": h["total"],
        "correct": h["correct"],
        "incorrect": h["incorrect"],
        "unattempted": h["unattempted"],
        "percentage": h["percentage"],
        "time_taken": h.get("time_taken", 0)
    } for h in reversed(history)]
    return jsonify(summary)


@app.route("/clear_history", methods=["POST"])
def clear_history():
    save_history([])
    return jsonify({"message": "History cleared"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
