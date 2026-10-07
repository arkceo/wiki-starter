# The image a cloud service runs each reading job in (cloud/job.py), built from this
# engine exactly as published: build it from a checkout of a wiki-starter release, with
# the checkout as the context, and nothing else goes in.
#
#   docker build -f cloud/Dockerfile -t wiki-job:<release tag> .
#
# oeru builds it this way inside its own AWS account (cloud/buildspec.yml) from the
# release it names on the workspace page, so anyone can rebuild it from the same tag.
FROM public.ecr.aws/docker/library/node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY . /opt/engine
RUN rm -rf /opt/engine/.git

# wiki_cloud.py links each wiki's node_modules and .venv to these (WIKI_DEPS).
RUN mkdir -p /opt/wiki-deps \
 && cp /opt/engine/package.json /opt/engine/package-lock.json /opt/wiki-deps/ \
 && cd /opt/wiki-deps && npm ci --no-audit --no-fund \
 && python3 -m venv /opt/wiki-deps/venv \
 && /opt/wiki-deps/venv/bin/pip install --no-cache-dir -r /opt/engine/scripts/requirements.txt

# The cloud starter pages over the Mac ones (wiki_cloud.py cloud-image says how).
RUN python3 /opt/engine/scripts/wiki_cloud.py cloud-image --engine /opt/engine

# job.py's only dependency: the AWS SDK, in a venv of its own.
RUN python3 -m venv /opt/job-venv && /opt/job-venv/bin/pip install --no-cache-dir 'boto3>=1.34,<2'

ENV WIKI_DEPS=/opt/wiki-deps WIKI_ENGINE_DIR=/opt/engine PYTHONDONTWRITEBYTECODE=1
CMD ["/opt/job-venv/bin/python", "/opt/engine/cloud/job.py"]
