#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
runtime_dir="${1:-${script_dir}/runtime}"
compose_env="${script_dir}/.env"

umask 077
mkdir -p \
    "${runtime_dir}/codex" \
    "${runtime_dir}/config" \
    "${runtime_dir}/state"
chmod 0700 \
    "${runtime_dir}" \
    "${runtime_dir}/codex" \
    "${runtime_dir}/config" \
    "${runtime_dir}/state"

if [ ! -e "${runtime_dir}/tutor.env" ]; then
    : > "${runtime_dir}/tutor.env"
fi
if [ ! -e "${runtime_dir}/worker.env" ]; then
    : > "${runtime_dir}/worker.env"
fi
chmod 0600 "${runtime_dir}/tutor.env" "${runtime_dir}/worker.env"

if [ -e "${compose_env}" ]; then
    echo "Preserving existing ${compose_env}"
else
    temp_env="${compose_env}.tmp"
    {
        echo "TUTOR_UID=$(id -u)"
        echo "TUTOR_GID=$(id -g)"
        echo "TUTOR_RUNTIME_DIR=${runtime_dir}"
    } > "${temp_env}"
    chmod 0600 "${temp_env}"
    mv "${temp_env}" "${compose_env}"
fi

echo "Prepared owner-only runtime directories in ${runtime_dir}"
echo "Next: docker compose --profile tools run --rm initializer"
