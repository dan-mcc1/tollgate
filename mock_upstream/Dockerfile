FROM python:3.13-slim

WORKDIR /app

COPY mock_upstream/requirements.txt mock_upstream/requirements.txt
RUN pip install --no-cache-dir -r mock_upstream/requirements.txt

COPY mock_upstream/ mock_upstream/

EXPOSE 8001

CMD ["uvicorn", "mock_upstream.main:app", "--host", "0.0.0.0", "--port", "8001"]
