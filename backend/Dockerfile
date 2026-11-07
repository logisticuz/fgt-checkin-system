# Base image: slimmed-down official Python 3.11 image
FROM python:3.11.10-slim-bookworm

# Set working directory inside the container
WORKDIR /app

# Copy Python dependencies list and install them
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Copy all app files into the container
COPY . .

# Start the FastAPI app using Uvicorn
# --reload is for development (auto-restart on file changes)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
