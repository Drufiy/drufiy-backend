# Use a Python base image suitable for slim deployments
FROM python:3.11-slim

# Set environment variables for non-interactive installs and unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install all dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Cloud Run uses the PORT environment variable, defaulting to 8080
EXPOSE 8080

# Run the application using Uvicorn
# Use PORT environment variable for Cloud Run compatibility (shell form for env var expansion)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 2"]
