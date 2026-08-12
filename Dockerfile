FROM python:3.10.20-slim

ARG BBP_GIT_INFO=""

WORKDIR /src
COPY requirements.txt /

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git rsync nodejs npm && \
    rm -rf /var/lib/apt/lists/* && \
    python -m venv /venv && \
    /venv/bin/python -m pip install --upgrade pip && \
    git config --global --add safe.directory '*' && \
    npm install jsonld && \
    echo "$BBP_GIT_INFO" > /GIT_INFO && \
    git config --system --add safe.directory '*'

RUN /venv/bin/python -m pip install -r /requirements.txt

# Apply rdflib fixes
RUN /venv/bin/python -m pip install git+https://github.com/avillar/rdflib.git@6.x

# Reference copy of bblocks-template, used to detect outdated scaffolding scripts
# (build.sh, view.sh, ...) in the repo being processed. A full (non-shallow)
# clone is needed to walk the files' full history when building the hash
# manifest below; .git is stripped afterwards so it isn't carried in the image.
COPY scripts/generate_template_hash_manifest.py /tmp/generate_template_hash_manifest.py
COPY ogc/bblocks/tracked_template_files.txt /tmp/tracked_template_files.txt
COPY ogc/bblocks/lineage_template_files.json /tmp/lineage_template_files.json
RUN git clone https://github.com/opengeospatial/bblocks-template.git /opt/bblocks-template && \
    /venv/bin/python /tmp/generate_template_hash_manifest.py \
        /opt/bblocks-template /opt/bblocks-template/.known-template-hashes.json \
        /tmp/tracked_template_files.txt /tmp/lineage_template_files.json && \
    rm -rf /opt/bblocks-template/.git /tmp/generate_template_hash_manifest.py \
        /tmp/tracked_template_files.txt /tmp/lineage_template_files.json

ENV PYTHONPATH=/src/
ENV PYTHONUNBUFFERED=1
ENV NODE_PATH=/src/node_modules
ENV BBP_GIT_INFO_FILE=/GIT_INFO
ENV BBP_TEMPLATE_DIR=/opt/bblocks-template

COPY ogc/ /src/ogc/

ENTRYPOINT ["/venv/bin/python", "-m", "ogc.bblocks.entrypoint"]
