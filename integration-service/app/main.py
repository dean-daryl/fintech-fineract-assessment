from fastapi import FastAPI

app = FastAPI(title="Integration Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "integration-service"}
