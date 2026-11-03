#!/bin/sh
# NXCT: installation and manual updates for Linux x86_64, without root access.
set -eu

REPO=https://github.com/sxxlxxr/nxct
ASSET=nxct-linux-x86_64.tar.gz
BASE=${XDG_DATA_HOME:-"$HOME/.local/share"}/nxct
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/nxct"
STAGE=
NEXT=

fail() {
    printf 'NXCT: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [ -n "$NEXT" ] && [ -L "$NEXT" ]; then
        rm -- "$NEXT"
    fi
    if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
        rm -rf -- "$STAGE"
    fi
}
trap cleanup 0

[ "$(uname -s)" = Linux ] || fail 'поддерживается только Linux'
[ "$(uname -m)" = x86_64 ] || fail 'пока поддерживается только x86_64'
[ "$(id -u)" != 0 ] || fail 'не запускайте установщик через sudo или от root'
for tool in curl tar sha256sum mktemp grep mv ln chmod; do
    command -v "$tool" >/dev/null 2>&1 || fail "нужна команда: $tool"
done

case "$#" in
    0) RELEASE=latest/download ;;
    2)
        [ "$1" = --version ] || fail 'использование: install.sh [--version vX.Y.Z]'
        printf '%s\n' "$2" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || fail 'неверная версия'
        RELEASE="download/$2" ;;
    *) fail 'использование: install.sh [--version vX.Y.Z]' ;;
esac

if [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; then
    [ -L "$LAUNCHER" ] && [ "$(readlink "$LAUNCHER")" = "$BASE/current/nxct/nxct" ] ||
        fail "существующий $LAUNCHER не принадлежит NXCT; файл не изменён"
fi
mkdir -p "$BASE/versions" "$BIN_DIR"
STAGE=$(mktemp -d "$BASE/.install.XXXXXXXX")
ARCHIVE="$STAGE/$ASSET"
SUMS="$STAGE/SHA256SUMS"

printf 'NXCT: скачиваю релиз %s…\n' "$RELEASE"
curl -fLsS --retry 2 --connect-timeout 10 --max-time 300 -o "$ARCHIVE" \
    "$REPO/releases/$RELEASE/$ASSET" || fail 'не удалось скачать релиз'
curl -fLsS --retry 2 --connect-timeout 10 --max-time 30 -o "$SUMS" \
    "$REPO/releases/$RELEASE/SHA256SUMS" || fail 'не удалось скачать контрольные суммы'
line=$(grep -E "^[0-9a-f]{64}  $ASSET\$" "$SUMS" || :)
[ -n "$line" ] || fail 'контрольная сумма релиза не найдена'
expected=${line%% *}
actual=$(sha256sum "$ARCHIVE")
actual=${actual%% *}
[ "$actual" = "$expected" ] || fail 'контрольная сумма не совпадает; установка отменена'

# Разрешаем в архиве только собственный каталог приложения и файл версии.
tar -tzf "$ARCHIVE" > "$STAGE/contents" || fail 'повреждённый архив'
while IFS= read -r item; do
    case "$item" in
        nxct|nxct/*|version.txt) ;;
        *) fail "неожиданный путь в архиве: $item" ;;
    esac
    case "$item" in
        */../*|*/..|*/./*|*//*) fail "небезопасный путь в архиве: $item" ;;
    esac
done < "$STAGE/contents"

mkdir "$STAGE/unpacked"
tar -xzf "$ARCHIVE" -C "$STAGE/unpacked" --no-same-owner --no-same-permissions ||
    fail 'не удалось распаковать архив'
[ -f "$STAGE/unpacked/version.txt" ] || fail 'в архиве нет версии'
version=$(cat "$STAGE/unpacked/version.txt")
printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || fail 'неверная версия внутри архива'
if [ "$RELEASE" != latest/download ] && [ "$RELEASE" != "download/v$version" ]; then
    fail 'версия архива не совпадает с запрошенной'
fi
[ -f "$STAGE/unpacked/nxct/nxct" ] && [ -f "$STAGE/unpacked/nxct/rqbit" ] ||
    fail 'в архиве нет NXCT или rqbit'
chmod u+x "$STAGE/unpacked/nxct/nxct" "$STAGE/unpacked/nxct/rqbit"

DEST="$BASE/versions/$version"
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
    [ -d "$DEST" ] && [ ! -L "$DEST" ] && [ -x "$DEST/nxct/nxct" ] &&
        [ -x "$DEST/nxct/rqbit" ] || fail "каталог версии $version уже существует и повреждён"
    printf 'NXCT: версия %s уже установлена.\n' "$version"
else
    mv -- "$STAGE/unpacked" "$DEST"
fi

NEXT="$BASE/.current.$$"
[ ! -e "$NEXT" ] && [ ! -L "$NEXT" ] || fail 'временная ссылка уже существует'
ln -s "versions/$version" "$NEXT"
mv -Tf -- "$NEXT" "$BASE/current" || fail 'не удалось переключить версию'
NEXT=
if [ ! -L "$LAUNCHER" ]; then
    ln -s "$BASE/current/nxct/nxct" "$LAUNCHER"
fi
printf 'NXCT %s установлен. Запуск: %s\n' "$version" "$LAUNCHER"
