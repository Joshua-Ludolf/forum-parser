# Use a lightweight Python 3.12 image
FROM python:3.12-slim

# Install uv directly from astral's official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Copy the dependency management files first to cache the layer
COPY pyproject.toml uv.lock ./

# Install dependencies into a virtual environment using uv
RUN uv sync --frozen --no-dev

# Copy the rest of the application code
COPY main.py .

# Set environment variables so Python runs optimally
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Run the bot using uv's managed virtual environment
CMD ["uv", "run", "main.py"]