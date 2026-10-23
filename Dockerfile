FROM python:3.12-slim

WORKDIR /app
COPY rooms/ ./rooms/

ENV ROOMS_HOST=0.0.0.0 \
    ROOMS_PORT=8775 \
    ROOMS_DATA=/data

EXPOSE 8775
CMD ["python", "-m", "rooms.server"]
