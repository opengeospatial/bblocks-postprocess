FROM python:3.10-alpine

WORKDIR /src
COPY requirements.txt metadata-schema.yaml /

RUN apk update && \
    apk add git && \
    python -m pip install --upgrade pip && \
    python -m pip install -r /requirements.txt

ENV PYTHONPATH /src/
ENV PYTHONUNBUFFERED 1

COPY ogc/ /src/ogc/

ENTRYPOINT ["python", "-m", "ogc.bblocks.entrypoint"]