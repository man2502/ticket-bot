#!/bin/sh
# pip install с первого индекса, который ответит.
#
#   pip-try.sh "<индекс> <индекс> ..." <аргументы pip install>
#
# В Туркменистане files.pythonhosted.org и download.pytorch.org то доступны,
# то нет («Connection refused», «Network is unreachable»), и сборка падала
# на первом же пакете. Зеркала копируют PyPI целиком, пакеты там те же.
# На каждый индекс — одна короткая попытка: недоступный не должен держать
# сборку минутами. Что уже поставлено с прошлого индекса, второй раз не
# качается — pip видит это сам.
set -u

indexes=$1
shift

for url in $indexes; do
    echo "pip: пробую $url"
    if pip install --retries 1 --timeout 30 --index-url "$url" "$@"; then
        exit 0
    fi
done

echo "pip: ни один индекс не ответил: $indexes" >&2
exit 1
