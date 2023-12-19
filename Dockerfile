FROM python:3.10-alpine

WORKDIR /src
COPY requirements.txt /

RUN apk update && \
    apk add git rsync nodejs npm && \
    python -m venv /venv && \
    /venv/bin/python -m pip install --upgrade pip && \
    git config --global --add safe.directory '*' && \
    npm install jsonld

RUN /venv/bin/python -m pip install -r /requirements.txt

ENV PYTHONPATH /src/
ENV PYTHONUNBUFFERED 1
ENV NODE_PATH=/src/node_modules

COPY ogc/ /src/ogc/

ENTRYPOINT ["/venv/bin/python", "-m", "ogc.bblocks.entrypoint"]