from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import pandas as pd
import os
import requests
from urllib.parse import quote

app = FastAPI()

# ✅ Dessa paths funkar eftersom ./backend mountas som /app i containern
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# CSV paths i containern (om du använder dem längre fram)
ebas_path = "/data/ebas_medlemmar.csv"
swish_path = "/data/swish_logs.csv"
startgg_path = "/data/startgg_reg.csv"

AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE = "Checkin"
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")

def check_participant_status(namn: str) -> dict:
    status = {"namn": namn, "summary": "❌ Saknas helt", "medlem": False, "swish": False, "startgg": False}
    headers = { "Authorization": f"Bearer {AIRTABLE_API_KEY}" }
    formula = f"LOWER(namn)='{namn.lower()}'"
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}?filterByFormula={quote(formula)}"

    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        records = response.json().get("records", [])
        if records:
            f = records[0]["fields"]
            status["medlem"] = f.get("medlem", False)
            status["swish"] = f.get("swish", False)
            status["startgg"] = f.get("startgg", False)

            if status["startgg"] and status["medlem"] and status["swish"]:
                status["summary"] = "✅ Klar för deltagande"
            elif not status["medlem"]:
                status["summary"] = "⏳ Saknar medlemskap"
            elif not status["swish"]:
                status["summary"] = "⏳ Saknar betalning"
            elif not status["startgg"]:
                status["summary"] = "⏳ Saknar turneringsregistrering"
            else:
                status["summary"] = "🟡 Delvis komplett"

    return status


@app.get("/status/{namn}", response_class=HTMLResponse)
async def status_view(request: Request, namn: str):
    status = check_participant_status(namn)
    template_name = "status_ready.html" if status["medlem"] and status["swish"] else "status_pending.html"
    return templates.TemplateResponse(template_name, {"request": request, "namn": namn, "status": status})


@app.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})
