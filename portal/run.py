"""一键启动：在 portal 目录下执行 `python run.py`。"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=False)
