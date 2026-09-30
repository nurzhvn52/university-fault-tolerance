# PostgreSQL 16 managed by Patroni; the cluster state (leader key) lives in etcd.
FROM postgres:16

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv curl \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/patroni \
    && /opt/patroni/bin/pip install --no-cache-dir "patroni[etcd3,psycopg3]==4.1.5"

COPY patroni.yml /etc/patroni.yml
COPY post_bootstrap.sh /usr/local/bin/post_bootstrap.sh
RUN chmod +x /usr/local/bin/post_bootstrap.sh

USER postgres
EXPOSE 5432 8008
HEALTHCHECK --interval=2s --timeout=2s --retries=15 \
    CMD curl -fs http://localhost:8008/liveness || exit 1
CMD ["/opt/patroni/bin/patroni", "/etc/patroni.yml"]
