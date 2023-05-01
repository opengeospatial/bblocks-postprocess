FROM python:3.10-alpine

WORKDIR /src
COPY requirements.txt /

RUN apk update && \
    apk add git rsync && \
    python -m venv /venv && \
    /venv/bin/python -m pip install --upgrade pip && \
    /venv/bin/python -m pip install -r /requirements.txt

ENV PYTHONPATH /src/
ENV PYTHONUNBUFFERED 1

COPY ogc/ /src/ogc/

ENTRYPOINT ["/venv/bin/python", "-m", "ogc.bblocks.entrypoint"]