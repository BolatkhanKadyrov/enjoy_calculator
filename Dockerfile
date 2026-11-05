# Release builds should additionally pin this tag by its verified registry digest.
ARG PYTHON_IMAGE=python:3.14.6-slim-bookworm
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS build
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements.lock
COPY manage.py ./manage.py
COPY config ./config
COPY tseh ./tseh
# The build settings use no production credentials and never connect to a DB.
RUN DJANGO_SETTINGS_MODULE=config.build_settings python manage.py collectstatic --noinput

FROM base AS runtime
WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /build/staticfiles ./staticfiles
COPY manage.py ./manage.py
COPY config ./config
COPY tseh ./tseh
COPY scripts/start-production.sh scripts/predeploy.py ./scripts/
COPY requirements.lock pyproject.toml ./
RUN sed -i 's/\r$//' scripts/start-production.sh
USER 10001:10001
EXPOSE 8000
CMD ["sh", "scripts/start-production.sh"]
