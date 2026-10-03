FROM odoo:20.0

USER root

COPY one_erp_bundle /tmp/one_erp_bundle

RUN cat /tmp/one_erp_bundle/part*.b64 | tr -d '\n\r' | base64 -d > /tmp/one_erp.zip \
    && python3 -m zipfile -e /tmp/one_erp.zip /tmp/one_erp \
    && mkdir -p /mnt/extra-addons \
    && cp -R /tmp/one_erp/ONE_Odoo20_RC2/addons/. /mnt/extra-addons/ \
    && chown -R odoo:odoo /mnt/extra-addons \
    && rm -rf /tmp/one_erp /tmp/one_erp.zip /tmp/one_erp_bundle

USER odoo

EXPOSE 8069

CMD ["odoo","--addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons","--proxy-mode","--without-demo=all","--workers=2","--max-cron-threads=1"]
