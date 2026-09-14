
import asyncio
import io
import re
import zipfile
from pathlib import Path

import edge_tts
import streamlit as st
from pypdf import PdfReader

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

    # 1) حذف الأسطر المتجاورة المتطابقة
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    unique_lines = []
    for line in lines:
        if unique_lines and norm(line) == norm(unique_lines[-1]):
            continue
        unique_lines.append(line)

    # 2) حذف الكلمات المتجاورة المتطابقة
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

    # 3) حذف المقاطع المتجاورة المتطابقة على مستوى الكلمات
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

    # 4) حذف العبارة المكررة المتجاورة على مستوى الأحرف
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


async def make_audio(text):
    out = io.BytesIO()
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(out)
    return out.getvalue()


def convert_pdf(uploaded_file, progress=None):
    reader = PdfReader(uploaded_file)
    total = len(reader.pages)
    audio_files = []
    skipped = 0

    for page_number, page in enumerate(reader.pages, start=1):
        if progress:
            progress(page_number, total)

        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""

        text = clean_extracted_text(text)
        if not text:
            skipped += 1
            continue

        try:
            data = asyncio.run(make_audio(text))
            audio_files.append((f"صفحة_{page_number:04d}.mp3", data))
        except Exception:
            skipped += 1

    return total, audio_files, skipped


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
    "هذه نسخة تعمل من خلال المتصفح: لا يحتاج المستخدم إلى ملف PY. "
    "يختار كتابًا أو عدة كتب PDF، ثم يضغط زر التحويل، ويحصل على ملفات MP3 داخل ملف ZIP."
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

            def update_page(page, total):
                page_progress.progress(page / max(total, 1))
                status.write(
                    f"الكتاب {book_index}/{len(files)} — "
                    f"{book_name} — الصفحة {page}/{total}"
                )

            try:
                total, audio_files, skipped = convert_pdf(uploaded, update_page)
                all_results.append((book_name, audio_files, total, skipped))
            except Exception as e:
                st.error(f"تعذر تحويل {uploaded.name}: {e}")

            overall.progress(book_index / len(files))

        if all_results:
            # ملف ZIP واحد يحتوي كل الكتب
            final_buffer = io.BytesIO()
            with zipfile.ZipFile(final_buffer, "w", zipfile.ZIP_DEFLATED) as z:
                for book_name, audio_files, _, _ in all_results:
                    for filename, data in audio_files:
                        z.writestr(f"{book_name}_صوتي/{filename}", data)

            final_buffer.seek(0)

            st.success("✅ اكتمل التحويل.")
            for book_name, audio_files, total, skipped in all_results:
                st.write(
                    f"**{book_name}** — {len(audio_files)} صفحة صوتية من {total} "
                    f"(تخطي {skipped})"
                )

            st.download_button(
                "⬇️ تنزيل الملفات الصوتية",
                data=final_buffer.getvalue(),
                file_name="الكتب_الصوتية.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )

st.caption("الصوت المستخدم: ar-EG-SalmaNeural — نفس الصوت العربي الموجود في النسخة الأصلية.")
