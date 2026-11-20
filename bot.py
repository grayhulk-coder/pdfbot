import os, io, re, json, asyncio, threading
from flask import Flask
from telegram import Update
from telegram.ext import (ApplicationBuilder, CommandHandler,
                          MessageHandler, filters, ContextTypes)
from google import genai
from google.genai import types
from fpdf import FPDF
from pypdf import PdfWriter, PdfReader

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ.get("OWNER_ID", "0"))  # your Telegram ID (locks bot to you)
client = genai.Client(api_key=os.environ["GEMINI_KEY"])
MODEL = "gemini-3.8-flash"  # change if AI Studio shows a newer free model

SYSTEM = """You are a smart all-round personal assistant inside Telegram, like ChatGPT. You can help with anything: studies, coding, writing, math, career, ideas, planning, translation, explanations. Give complete, high-quality answers.
Reply ONLY with JSON in this shape:
{"action":"pdf|chat|remind","reply":"short message to user","title":"pdf title","content":"full document text","minutes":0,"task":"reminder text"}
- Use "pdf" when the user wants a document, notes, assignment, report, summary, answers, resume, or any file. Put the COMPLETE content in "content" using plain text, '# ' for headings and '- ' for bullets.
- Use "remind" when the user wants a reminder. Set "minutes" (from now) and "task".
- Otherwise use "chat" and answer in "reply".
Always fill "reply" with a one-line message."""

web = Flask(__name__)

@web.route("/")
def home():
    return "Bot is running"

def run_web():
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

def clean(t):
    t = re.sub(r"[*`_]", "", t)
    return t.encode("latin-1", "replace").decode("latin-1")

def make_pdf(title, content):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(True, 15)
    pdf.set_font("Helvetica", "B", 18)
    pdf.multi_cell(0, 10, clean(title) or " ", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    for line in content.split("\n"):
        line = line.rstrip()
        if line.startswith("#"):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 14)
            pdf.multi_cell(0, 8, clean(line.lstrip("# ")) or " ",
                           new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.set_font("Helvetica", size=12)
            pdf.multi_cell(0, 7, clean(line) or " ",
                           new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())

def ask(prompt, image=None):
    parts = [prompt]
    if image:
        parts.append(types.Part.from_bytes(data=image, mime_type="image/jpeg"))
    r = client.models.generate_content(
        model=MODEL, contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json"))
    return json.loads(r.text)

def ok(update):
    return OWNER == 0 or update.effective_user.id == OWNER

async def remind_cb(ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_message(ctx.job.chat_id, "Reminder: " + ctx.job.data)

async def myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your ID: {update.effective_user.id}")

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    await update.message.reply_text(
        "Just tell me what you need:\n"
        "- 'Make a PDF on OS scheduling, 2 pages'\n"
        "- 'Remind me in 30 minutes to submit assignment'\n"
        "- Send a PDF with a caption (e.g. 'summarize') to work on it\n"
        "- Send PDFs without caption, then /merge\n"
        "/clear empties queued PDFs.")

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    await update.message.reply_text("Working on it...")
    try:
        h = ctx.user_data.setdefault("h", [])
        convo = "\n".join(h[-16:])
        d = await asyncio.to_thread(
            ask, f"Conversation so far:\n{convo}\n\nUser: {update.message.text}")
        h.append("User: " + update.message.text[:1500])
        h.append("Assistant: " + (d.get("reply") or d.get("title") or "")[:1500])
        act = d.get("action", "chat")
        if act == "pdf":
            pdf = make_pdf(d.get("title", "Document"), d.get("content", ""))
            await update.message.reply_document(
                io.BytesIO(pdf), filename="document.pdf",
                caption=d.get("reply", ""))
        elif act == "remind":
            mins = float(d.get("minutes", 1))
            ctx.job_queue.run_once(
                remind_cb, mins * 60, chat_id=update.effective_chat.id,
                data=d.get("task", "Reminder"))
            await update.message.reply_text(
                f"Done. I'll remind you in {mins:g} min: {d.get('task','')}")
        else:
            await update.message.reply_text(d.get("reply", "..."))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def on_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    doc = update.message.document
    if doc.mime_type != "application/pdf":
        await update.message.reply_text("Please send a PDF file.")
        return
    f = await doc.get_file()
    data = bytes(await f.download_as_bytearray())
    caption = update.message.caption
    if caption:  # work on this PDF
        await update.message.reply_text("Reading it...")
        try:
            text = "\n".join((p.extract_text() or "")
                             for p in PdfReader(io.BytesIO(data)).pages)[:30000]
            d = await asyncio.to_thread(
                ask, f"{caption}\n\nPDF CONTENT:\n{text}")
            if d.get("action") == "pdf":
                pdf = make_pdf(d.get("title", "Result"), d.get("content", ""))
                await update.message.reply_document(
                    io.BytesIO(pdf), filename="result.pdf",
                    caption=d.get("reply", ""))
            else:
                await update.message.reply_text(d.get("reply", "..."))
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
    else:  # queue for merge
        ctx.user_data.setdefault("pdfs", []).append(data)
        n = len(ctx.user_data["pdfs"])
        await update.message.reply_text(f"{n} PDF(s) queued. Send more or /merge")

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    await update.message.reply_text("Looking at it...")
    try:
        f = await update.message.photo[-1].get_file()
        img = bytes(await f.download_as_bytearray())
        q = update.message.caption or "Describe this image and help me with it."
        d = await asyncio.to_thread(ask, q, img)
        if d.get("action") == "pdf":
            pdf = make_pdf(d.get("title", "Result"), d.get("content", ""))
            await update.message.reply_document(
                io.BytesIO(pdf), filename="result.pdf",
                caption=d.get("reply", ""))
        else:
            await update.message.reply_text(d.get("reply", "..."))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def merge(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    pdfs = ctx.user_data.get("pdfs", [])
    if len(pdfs) < 2:
        await update.message.reply_text("Send at least 2 PDFs first.")
        return
    w = PdfWriter()
    for p in pdfs:
        w.append(io.BytesIO(p))
    out = io.BytesIO()
    w.write(out)
    out.seek(0)
    ctx.user_data["pdfs"] = []
    await update.message.reply_document(out, filename="merged.pdf")

async def clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["pdfs"] = []
    ctx.user_data["h"] = []
    await update.message.reply_text("Cleared PDFs and chat memory.")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("merge", merge))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()
