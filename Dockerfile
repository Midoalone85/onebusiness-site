FROM odoo:20.0

USER root

RUN echo 'deb http://apt.postgresql.org/pub/repos/apt/ noble-pgdg main' > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends postgresql-16 python3-markdown2 \
    && rm -f /etc/apt/sources.list.d/pgdg.list \
    && rm -rf /var/lib/apt/lists/*

COPY one_erp_bundle /tmp/one_erp_bundle
COPY one_custom /tmp/one_custom
COPY start-one-render.sh /usr/local/bin/start-one-render.sh

RUN cat /tmp/one_erp_bundle/part*.b64 | tr -d '\n\r' | base64 -d > /tmp/one_erp.zip \
    && python3 -m zipfile -e /tmp/one_erp.zip /tmp/one_erp \
    && mkdir -p /mnt/extra-addons \
    && cp -R /tmp/one_erp/ONE_Odoo20_RC2/addons/. /mnt/extra-addons/ \
    && cp -R /tmp/one_custom/. /mnt/extra-addons/ \
    && chmod +x /usr/local/bin/start-one-render.sh \
    && chown -R odoo:odoo /mnt/extra-addons /usr/local/bin/start-one-render.sh \
    && rm -rf /tmp/one_erp /tmp/one_erp.zip /tmp/one_erp_bundle /tmp/one_custom

USER odoo

EXPOSE 10000

ENTRYPOINT ["/usr/local/bin/start-one-render.sh"]
