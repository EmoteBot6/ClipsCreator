FROM python:3.10

WORKDIR /workspace/frontend

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "index.py"] 
