from fastapi import FastAPI
app = FastAPI()

@app.get("/")
def read_root():
    return {"msg": "Checkin system is live!"}
# för att köra uvicorn backend.app.main:app --reload
