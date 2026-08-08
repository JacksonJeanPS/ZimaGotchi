FROM alpine:3.20
RUN apk add --no-cache python3 tshark wireshark-common iw iproute2 \
    && command -v tshark \
    && command -v dumpcap
WORKDIR /app
COPY app.py index.html networks.json /app/
EXPOSE 8686
CMD ["python3", "/app/app.py"]
