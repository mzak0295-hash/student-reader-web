import asyncio
import io
import os
import re
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import edge_tts
import streamlit as st
from pypdf import PdfReader

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


VOICE = "ar-EG-SalmaNeural"


def clean_extracted_text(text):
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)

    def bare(s):
        return re.sub(
            r"^[،؛,:.!؟()\[\]{}\"'«»]+|[،؛,:.!؟()\[\]{}\"'«»]+$",
            "",
            s,
        ).strip()

    def norm(s):
        return re.sub(r"\s+", " ", s).strip()

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    unique_lines = []
    for line in lines:
        if unique_lines and norm(line) == norm(unique_lines[-1]):
            continue
        unique_lines.append(line)

    fixed_lines = []
    for line in unique_lines:
        words = line.split()
        fixed = []
        for word in words:
            if fixed and bare(word) and bare(word) == bare(fixed[-1]):
                continue
            fixed.append(word)
        line2 = " ".join(fixed).strip()
        if line2:
            fixed_lines.append(line2)

    text = "\n".join(fixed_lines).strip()

    words = text.replace("\n", " ").split()
    if len(words) >= 6:
        cleaned_words = []
        i = 0
        n = len(words)
        while i < n:
            found = False
            max_len = min(80, (n - i) // 2)
            for length in range(max_len, 2, -1):
                first = [bare(w) for w in words[i:i + length]]
                second = [bare(w) for w in words[i + length:i + 2 * length]]
                if first == second:
                    cleaned_words.extend(words[i:i + length])
                    i += 2 * length
                    found = True
                    break
            if not found:
                cleaned_words.append(words[i])
                i += 1
        text = " ".join(cleaned_words).strip()

    text = re.sub(r"[ \t]+", " ", text)
    for _ in range(5):
        changed = False
        for length in range(180, 5, -1):
            pattern = re.compile(r"(.{" + str(length) + r"})(?:\1)", re.DOTALL)
            new_text, count = pattern.subn(r"\1", text)
            if count:
                text = new_text
                changed = True
                break
        if not changed:
            break

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def ocr_page(page):
    """OCR عربي قوي للصفحات التي لا يظهر منها نص قابل للاستخراج.

    نستخدم OCR المدمج في PyMuPDF أولاً، ثم نستخدم Tesseract مباشرة
    كخطة احتياطية. لا نعتمد على pytesseract أو Pillow حتى يعمل التطبيق
    مع requirements.txt الموجود فعلاً في المستودع.
    """
    if fitz is None:
        return ""

    # الطريقة الأولى: OCR المدمج في PyMuPDF + Tesseract النظامي.
    try:
        if hasattr(page, "get_textpage_ocr"):
            tp = page.get_textpage_ocr(language="ara", dpi=220, full=True)
            text = page.get_text("text", textpage=tp, sort=True) or ""
            text = clean_extracted_text(text)
            if len(text.strip()) >= 8:
                return text
    except Exception:
        pass

    # خطة احتياطية: تشغيل tesseract مباشرة على صورة الصفحة.
    image_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            image_path = tmp.name
        pix = page.get_pixmap(matrix=fitz.Matrix(220 / 72, 220 / 72), alpha=False)
        pix.save(image_path)
        result = subprocess.run(
            ["tesseract", image_path, "stdout", "-l", "ara", "--oem", "1", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        text = clean_extracted_text(result.stdout or "")
        return text if len(text.strip()) >= 8 else ""
    except Exception:
        return ""
    finally:
        if image_path:
            try:
                os.remove(image_path)
            except OSError:
                pass

def extract_page_texts(pdf_bytes):
    """استخراج النص بأكثر من محرك، ثم OCR عربي للصفحات التي بقيت فارغة."""
    texts = []

    pypdf_reader = None
    try:
        pypdf_reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        pass

    doc = None
    if fitz is not None:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception:
            doc = None

    if pypdf_reader is not None:
        total = len(pypdf_reader.pages)
    elif doc is not None:
        total = len(doc)
    else:
        raise RuntimeError("تعذر فتح ملف PDF")

    for i in range(total):
        candidates = []

        if doc is not None:
            try:
                candidates.append(doc.load_page(i).get_text("text", sort=True) or "")
            except Exception:
                pass

        if pypdf_reader is not None:
            try:
                candidates.append(pypdf_reader.pages[i].extract_text() or "")
            except Exception:
                pass

        cleaned = [clean_extracted_text(x) for x in candidates]
        text = max(cleaned, key=len) if cleaned else ""

        # إذا كانت الصفحة صورة ممسوحة ضوئيًا، لا نتخطاها: نستخدم OCR العربي.
        if len(text.strip()) < 8 and doc is not None:
            text = ocr_page(doc.load_page(i))

        texts.append(text)

    if doc is not None:
        doc.close()
    return texts


async def make_audio(text):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            temp_path = tmp.name

        communicate = edge_tts.Communicate(text, VOICE)
        await communicate.save(temp_path)

        if not os.path.isfile(temp_path) or os.path.getsize(temp_path) < 1024:
            raise RuntimeError("لم يتم إنشاء ملف MP3 صالح")

        with open(temp_path, "rb") as audio_file:
            return audio_file.read()
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def convert_pdf(uploaded_file, progress=None):
    pdf_bytes = uploaded_file.getvalue()
    texts = extract_page_texts(pdf_bytes)
    total = len(texts)
    audio_files = []
    skipped_pages = []

    for page_number, text in enumerate(texts, start=1):
        if progress:
            progress(page_number, total, "تحضير الصفحة")

        if not text:
            skipped_pages.append(page_number)
            continue

        data = None
        for attempt in range(3):
            try:
                data = asyncio.run(make_audio(text))
                break
            except Exception:
                if attempt < 2:
                    time.sleep(1.5)

        if data is not None:
            audio_files.append((f"صفحة_{page_number:04d}.mp3", data))
        else:
            skipped_pages.append(page_number)

        if progress:
            progress(page_number, total, "تحويل الصفحة")

    return total, audio_files, skipped_pages


def make_zip(book_name, audio_files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for filename, data in audio_files:
            z.writestr(f"{book_name}_صوتي/{filename}", data)
    buffer.seek(0)
    return buffer.getvalue()


st.set_page_config(
    page_title="القارئ التعليمي للطلاب المكفوفين",
    page_icon="🔊",
    layout="centered",
)

st.markdown(
    """
    <style>
    html, body, [class*="css"] { direction: rtl; }
    .big-title { font-size: 2rem; font-weight: 800; text-align: center; }
    .sub-title { text-align: center; font-size: 1.1rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="big-title">🔊 القارئ التعليمي للطلاب المكفوفين</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">تحويل الكتب التعليمية PDF إلى ملفات صوتية عربية</div>', unsafe_allow_html=True)

st.info(
    "نسخة محسنة: تستخدم استخراج النص من أكثر من محرك، وتستخدم OCR عربي تلقائيًا "
    "للصفحات المصورة أو الصفحات التي لا يظهر منها نص."
)

mode = st.radio(
    "طريقة الاستخدام",
    ["تحويل كتاب واحد", "تحويل عدة كتب دفعة واحدة"],
    horizontal=False,
)

if mode == "تحويل كتاب واحد":
    files = st.file_uploader(
        "📚 اختر كتاب PDF",
        type=["pdf"],
        accept_multiple_files=False,
    )
else:
    files = st.file_uploader(
        "📚 اختر الكتب PDF التي تريد تحويلها",
        type=["pdf"],
        accept_multiple_files=True,
        help="يمكن تحديد عدة كتب من الهاتف أو الكمبيوتر.",
    )

if files:
    if not isinstance(files, list):
        files = [files]

    st.write(f"**عدد الكتب المختارة: {len(files)}**")

    if st.button("🔊 بدء تحويل الكتب إلى صوت", type="primary", use_container_width=True):
        all_results = []
        overall = st.progress(0)
        status = st.empty()

        for book_index, uploaded in enumerate(files, start=1):
            book_name = Path(uploaded.name).stem
            status.write(f"جاري تحويل الكتاب {book_index} من {len(files)}: **{uploaded.name}**")
            page_progress = st.progress(0)

            def update_page(page, total, stage):
                page_progress.progress(page / max(total, 1))
                status.write(
                    f"الكتاب {book_index}/{len(files)} — {book_name} — "
                    f"الصفحة {page}/{total} — {stage}"
                )

            try:
                total, audio_files, skipped_pages = convert_pdf(uploaded, update_page)
                all_results.append((book_name, audio_files, total, skipped_pages))
            except Exception as e:
                st.error(f"تعذر تحويل {uploaded.name}: {e}")

            overall.progress(book_index / len(files))

        if all_results:
            final_buffer = io.BytesIO()
            with zipfile.ZipFile(final_buffer, "w", zipfile.ZIP_DEFLATED) as z:
                for book_name, audio_files, _, _ in all_results:
                    for filename, data in audio_files:
                        z.writestr(f"{book_name}_صوتي/{filename}", data)

            final_buffer.seek(0)
            st.success("✅ اكتمل التحويل.")

            for book_name, audio_files, total, skipped_pages in all_results:
                if skipped_pages:
                    st.warning(
                        f"{book_name} — {len(audio_files)} صفحة صوتية من {total}. "
                        f"الصفحات التي تعذر تحويلها: {', '.join(map(str, skipped_pages))}"
                    )
                else:
                    st.success(f"{book_name} — تم تحويل جميع الصفحات {total}/{total} بنجاح ✅")

            st.download_button(
                "⬇️ تنزيل الملفات الصوتية",
                data=final_buffer.getvalue(),
                file_name="الكتب_الصوتية.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )

st.caption("الصوت المستخدم: ar-EG-SalmaNeural — نفس الصوت العربي الموجود في النسخة الأصلية.")
