#!/usr/bin/env bash
# k3s 서버(gusal-cloud)에서 실행: 이미지 빌드 → containerd 에 넣기 → 적용 → 재시작.
# 레지스트리를 쓰지 않으므로 파드가 뜨는 노드에서 빌드해야 한다 (deployment.yaml 의 nodeSelector).
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=ktx-bot:latest

if [ ! -f k8s/ktx/secret.yaml ]; then
  echo "k8s/ktx/secret.yaml 이 없습니다. secret.example.yaml 을 복사해 값을 채우세요." >&2
  exit 1
fi

docker build -t "$IMAGE" .
docker save "$IMAGE" | sudo k3s ctr images import -
sudo kubectl apply -k k8s/ktx
sudo kubectl -n apps rollout restart deploy/ktx-bot
sudo kubectl -n apps rollout status deploy/ktx-bot --timeout=180s
sudo kubectl -n apps logs deploy/ktx-bot --tail=20
