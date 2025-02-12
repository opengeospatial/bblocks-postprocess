FROM python:3.10-alpine3.15

ARG BBP_GIT_INFO=""

WORKDIR /src
COPY requirements.txt /

RUN apk update && \
    apk upgrade && \
    apk add git rsync nodejs npm && \
    python -m venv /venv && \
    /venv/bin/python -m pip install --upgrade pip && \
    git config --global --add safe.directory '*' && \
    npm install jsonld && \
    echo "$BBP_GIT_INFO" > /GIT_INFO && \
    git config --system --add safe.directory '*'

RUN /venv/bin/python -m pip install -r /requirements.txt

# Apply rdflib fixes
RUN /venv/bin/python -m pip install git+https://github.com/avillar/rdflib.git@6.x

ENV PYTHONPATH=/src/
ENV PYTHONUNBUFFERED=1
ENV NODE_PATH=/src/node_modules
ENV BBP_GIT_INFO_FILE=/GIT_INFO

COPY ogc/ /src/ogc/

ENTRYPOINT ["/venv/bin/python", "-m", "ogc.bblocks.entrypoint"]