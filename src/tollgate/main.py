from fastapi import FastAPI

app = FastAPI(title="Tollgate")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
