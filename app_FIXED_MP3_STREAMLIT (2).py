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
    import fitz
except ImportError:
    fitz = None

VOICE = "ar-EG-SalmaNeural"


def clean_extracted_text(text):
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    unique = []
    for line in lines:
        if unique and re.sub(r"\s+", " ", line) == re.sub(r"\s+", " ", unique[-1]):
            continue
        unique.append(line)
    text = "\n".join(unique).strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def ocr_page(page):
    if fitz is None:
        return ""
    image_path = None
    try:
        pix = page.get_pixmap(
            matrix=fitz.Matrix(220 / 72, 220 / 72),
            colorspace=fitz.csGRAY,
            alpha=False,
        )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            image_path = tmp.name
        pix.save(image_path)
        best = ""
        for psm in ("6", "3", "4"):
            try:
                result = subprocess.run(
                    ["tesseract", image_path, "stdout", "-l", "ara", "--oem", "1", "--psm", psm],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=90,
                )
                candidate = clean_extracted_text(result.stdout or "")
                if len(candidate) > len(best):
                    best = candidate
            except Exception:
                pass
            if len(best) >= 30:
                break
        if len(best) < 8:
            try:
                textpage = page.get_textpage_ocr(language="ara", dpi=220, full=True)
                best = max(best, clean_extracted_text(page.get_text("text", textpage=textpage) or ""), key=len)
            except Exception:
                pass
        return best
    finally:
        if image_path:
            try:
                os.remove(image_path)
            except OSError:
                pass


def extract_page_texts(pdf_bytes):
    reader = None
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        pass
    doc = None
    if fitz is not None:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception:
            pass
    if reader is not None:
        total = len(reader.pages)
    elif doc is not None:
        total = len(doc)
    else:
        raise RuntimeError("تعذر فتح ملف PDF")
    texts = []
    for i in range(total):
        candidates = []
        if doc is not None:
            try:
                candidates.append(doc.load_page(i).get_text("text", sort=True) or "")
            except Exception:
                pass
        if reader is not None:
            try:
                candidates.append(reader.pages[i].extract_text() or "")
            except Exception:
                pass
        cleaned = [clean_extracted_text(x) for x in candidates]
        text = max(cleaned, key=len) if cleaned else ""
        if len(text) < 8 and doc is not None:
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
        await edge_tts.Communicate(text, VOICE).save(temp_path)
        if not os.path.isfile(temp_path) or os.path.getsize(temp_path) < 1024:
            raise RuntimeError("لم يتم إنشاء ملف MP3 صالح")
        with open(temp_path, "rb") as f:
            return f.read()
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def convert_pdf(uploaded_file, progress=None):
    texts = extract_page_texts(uploaded_file.getvalue())
    total = len(texts)
    audio_files = []
    unreadable_pages = []
    for page_number, text in enumerate(texts, 1):
        if progress:
            progress(page_number, total, "تحضير الصفحة")
        if not text:
            text = f"الصفحة {page_number}. تعذر استخراج النص المقروء من هذه الصفحة آليًا."
            unreadable_pages.append(page_number)
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
            unreadable_pages.append(page_number)
        if progress:
            progress(page_number, total, "تحويل الصفحة")
    return total, audio_files, sorted(set(unreadable_pages))


st.set_page_config(page_title="القارئ التعليمي للطلاب المكفوفين", page_icon="🔊", layout="centered")
st.markdown("<style>html,body,[class*=\"css\"]{direction:rtl}.big-title{font-size:2rem;font-weight:800;text-align:center}.sub-title{text-align:center;font-size:1.1rem}</style>", unsafe_allow_html=True)
st.markdown('<div class="big-title">🔊 القارئ التعليمي للطلاب المكفوفين</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">تحويل الكتب التعليمية PDF إلى ملفات صوتية عربية</div>', unsafe_allow_html=True)
st.info("نسخة نهائية محسنة: استخراج النص + OCR عربي مباشر للصفحات المصورة، مع إنشاء ملف صوتي لكل صفحة.")
mode = st.radio("طريقة الاستخدام", ["تحويل كتاب واحد", "تحويل عدة كتب دفعة واحدة"], horizontal=False)
files = st.file_uploader("📚 اختر كتاب PDF" if mode == "تحويل كتاب واحد" else "📚 اختر الكتب PDF التي تريد تحويلها", type=["pdf"], accept_multiple_files=(mode != "تحويل كتاب واحد"))
if files:
    if not isinstance(files, list):
        files = [files]
    st.write(f"**عدد الكتب المختارة: {len(files)}**")
    if st.button("🔊 بدء تحويل الكتب إلى صوت", type="primary", use_container_width=True):
        all_results = []
        overall = st.progress(0)
        status = st.empty()
        for book_index, uploaded in enumerate(files, 1):
            book_name = Path(uploaded.name).stem
            page_progress = st.progress(0)
            def update_page(page, total, stage):
                page_progress.progress(page / max(total, 1))
                status.write(f"الكتاب {book_index}/{len(files)} — {book_name} — الصفحة {page}/{total} — {stage}")
            try:
                total, audio_files, unreadable = convert_pdf(uploaded, update_page)
                all_results.append((book_name, audio_files, total, unreadable))
            except Exception as e:
                st.error(f"تعذر تحويل {uploaded.name}: {e}")
            overall.progress(book_index / len(files))
        if all_results:
            final_buffer = io.BytesIO()
            with zipfile.ZipFile(final_buffer, "w", zipfile.ZIP_DEFLATED) as z:
                for book_name, audio_files, _, _ in all_results:
                    for filename, data in audio_files:
                        z.writestr(f"{book_name}_صوتي/{filename}", data)
            st.success("✅ اكتمل التحويل.")
            for book_name, audio_files, total, unreadable in all_results:
                if unreadable:
                    st.warning(f"{book_name} — تم إنشاء {len(audio_files)} ملفًا صوتيًا من {total}. الصفحات التي لم يُستخرج نصها: {', '.join(map(str, unreadable))}")
                else:
                    st.success(f"{book_name} — تم تحويل جميع الصفحات {total}/{total} بنجاح ✅")
            st.download_button("⬇️ تنزيل الملفات الصوتية", data=final_buffer.getvalue(), file_name="الكتب_الصوتية.zip", mime="application/zip", type="primary", use_container_width=True)
st.caption("الصوت المستخدم: ar-EG-SalmaNeural — نفس الصوت العربي الموجود في النسخة الأصلية.")
