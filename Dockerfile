FROM python:3.10-alpine

WORKDIR /src
COPY requirements.txt /

RUN apk update && \
    apk add git rsync npm && \
    python -m venv /venv && \
    /venv/bin/python -m pip install --upgrade pip && \
    /venv/bin/python -m pip install -r /requirements.txt && \
    /venv/bin/python -m pip install -r https://raw.githubusercontent.com/avillar/jsf/main/jsf/requirements.txt

ENV PYTHONPATH /src/
ENV PYTHONUNBUFFERED 1

COPY ogc/ /src/ogc/

RUN npm install --prefix ogc/bblocks/schema-faker/

ENTRYPOINT ["/venv/bin/python", "-m", "ogc.bblocks.entrypoint"]