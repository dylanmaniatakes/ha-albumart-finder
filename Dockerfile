ARG BUILD_FROM
FROM $BUILD_FROM

# Add-on metadata
ENV \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

# Dependencies
RUN apk add --no-cache python3 py3-pip

# Python deps
RUN pip3 install --no-cache-dir flask paho-mqtt requests

# App files
WORKDIR /opt/albumart
COPY run.py /opt/albumart/run.py

# s6 service
COPY rootfs /

# Expose UI port
EXPOSE 8099
