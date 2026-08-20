#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
runtime_dir="${script_dir}/runtime"
domain=""
compose_env="${script_dir}/.env"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --domain)
            [ "$#" -ge 2 ] || { echo "--domain requires a public hostname" >&2; exit 2; }
            domain=$2
            shift 2
            ;;
        --help)
            echo "Usage: ./prepare-compose.sh [runtime-dir] [--domain tutor.example.com]"
            exit 0
            ;;
        --*)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
        *)
            runtime_dir=$1
            shift
            ;;
    esac
done

source_revision=${SOURCE_REVISION:-}
if [ -z "${source_revision}" ]; then
    source_revision=$(git -C "${script_dir}/.." rev-parse HEAD)
fi
if ! printf '%s\n' "${source_revision}" | grep -Eq '^[0-9a-f]{40}$'; then
    echo "Could not determine the exact source commit" >&2
    exit 1
fi

umask 077
mkdir -p \
    "${runtime_dir}/codex" \
    "${runtime_dir}/config" \
    "${runtime_dir}/grader-run" \
    "${runtime_dir}/state"
chmod 0700 \
    "${runtime_dir}" \
    "${runtime_dir}/codex" \
    "${runtime_dir}/config" \
    "${runtime_dir}/grader-run" \
    "${runtime_dir}/state"

if [ ! -e "${runtime_dir}/tutor.env" ]; then
    : > "${runtime_dir}/tutor.env"
fi
if [ ! -e "${runtime_dir}/worker.env" ]; then
    : > "${runtime_dir}/worker.env"
fi
if [ ! -e "${runtime_dir}/grader.env" ]; then
    : > "${runtime_dir}/grader.env"
fi
chmod 0600 \
    "${runtime_dir}/tutor.env" \
    "${runtime_dir}/worker.env" \
    "${runtime_dir}/grader.env"

if [ -e "${compose_env}" ] && { [ ! -f "${compose_env}" ] || [ -L "${compose_env}" ]; }; then
    echo "Refusing to replace nonregular Compose environment at ${compose_env}" >&2
    exit 1
fi
if [ -z "${domain}" ] && [ -f "${compose_env}" ]; then
    domain=$(sed -n 's/^TUTOR_DOMAIN=//p' "${compose_env}" | tail -n 1)
fi
if [ -n "${domain}" ] && ! printf '%s\n' "${domain}" | grep -Eq \
    '^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$'; then
    echo "--domain must be a fully qualified public hostname" >&2
    exit 2
fi

temp_env="${compose_env}.tmp"
{
    if [ -f "${compose_env}" ]; then
        sed \
            -e '/^TUTOR_UID=/d' \
            -e '/^TUTOR_GID=/d' \
            -e '/^TUTOR_RUNTIME_DIR=/d' \
            -e '/^SOURCE_REVISION=/d' \
            -e '/^TUTOR_DOMAIN=/d' \
            "${compose_env}"
    fi
    echo "TUTOR_UID=$(id -u)"
    echo "TUTOR_GID=$(id -g)"
    echo "TUTOR_RUNTIME_DIR=${runtime_dir}"
    echo "SOURCE_REVISION=${source_revision}"
    if [ -n "${domain}" ]; then
        echo "TUTOR_DOMAIN=${domain}"
    fi
} > "${temp_env}"
chmod 0600 "${temp_env}"
mv "${temp_env}" "${compose_env}"

echo "Prepared owner-only runtime directories in ${runtime_dir}"
echo "Pinned build and runtime to source commit ${source_revision}"
if [ -n "${domain}" ]; then
    echo "Configured automatic HTTPS for ${domain}"
fi
echo "Next: docker compose --profile tools run --rm initializer"
