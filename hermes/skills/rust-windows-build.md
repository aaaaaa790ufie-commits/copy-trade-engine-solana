# rust-windows-build

Build Rust projects on Windows from git-bash/MSYS2 without Visual Studio — toolchain selection, MinGW-w64 setup, OpenSSL for windows-gnu, TLS/rustls quirks, and PATH conflicts.

## When to use

- Compiling Rust crates that depend on OpenSSL (solana-sdk, aws-sdk, etc.)
- Using `x86_64-pc-windows-gnu` target to avoid MSVC toolchain requirements
- Building tokio-based async projects with WebSocket dependencies
- Encountering `link.exe: extra operand` errors (MSYS/MSVC link.exe conflict)
- `dlltool.exe: CreateProcess` failures during cargo build

## Step 1: Choose your toolchain

Two targets exist for Windows in Rust:

| Target | Linker | Requires | Best for |
|--------|--------|----------|----------|
| `x86_64-pc-windows-msvc` | MSVC `link.exe` | Visual Studio Build Tools | Production, best performance |
| `x86_64-pc-windows-gnu` | GNU `ld` (from mingw-w64) | MinGW-w64 toolchain | No VS installed, CI/dev |

**If you don't have Visual Studio**, install the GNU target:

```bash
rustup toolchain install stable-x86_64-pc-windows-gnu
rustup default stable-x86_64-pc-windows-gnu
```

## Step 2: Install MinGW-w64 toolchain

```bash
curl -k -L -o /c/Users/Admin/mingw-w64.7z "https://github.com/niXman/mingw-builds-binaries/releases/download/14.2.0-rt_v12-rev1/x86_64-14.2.0-release-win32-seh-ucrt-rt_v12-rev1.7z"
"/c/Program Files/7-Zip/7z.exe" x "C:\\Users\\Admin\\mingw-w64.7z" -o"C:\\Users\\Admin\\mingw-tools-full" -y
export PATH="/c/Users/Admin/mingw-tools-full/mingw64/bin:$PATH"
```

## Step 3: OpenSSL for windows-gnu

```bash
# Check MSYS2 repo for latest version
python -c "
import urllib.request, re
url = 'https://repo.msys2.org/mingw/mingw64/'
html = urllib.request.urlopen(url + '?C=N;O=D').read().decode()
matches = re.findall(r'<a href=\"(mingw-w64-x86_64-openssl-[\d\.]+-\d+-any\.pkg\.tar\.zst)\"', html)
if matches:
    print(f'Latest: {url}{matches[0]}')
"

# Download and extract
"/c/Program Files/7-Zip/7z.exe" x "C:\\Users\\Admin\\openssl-pkg.tar.zst" -o"C:\\Users\\Admin\\openssl-extracted" -y
"/c/Program Files/7-Zip/7z.exe" x "C:\\Users\\Admin\\openssl-extracted\\openssl-pkg.tar" -o"C:\\Users\\Admin\\openssl-mingw" -y
export OPENSSL_DIR="C:\\Users\\Admin\\openssl-mingw\\mingw64"
```

## TLS/WebSocket quirks

**Decision table:**

| Scenario | TLS backend | Reason |
|----------|-------------|--------|
| Public Solana RPC (WS + HTTP) | `native-tls` | ZeroSSL cert not in webpki-roots |
| Helius / Alchemy / QuickNode | `__rustls-tls` or `native-tls` | Both work |
| Enterprise with internal CA | `native-tls` | Uses OS trust store |

## Full build command

```bash
export PATH="/c/Users/Admin/mingw-tools-full/mingw64/bin:$PATH:/c/Users/Admin/.cargo/bin"
export OPENSSL_DIR="C:\\Users\\Admin\\openssl-mingw\\mingw64"
cd ~/my-project
cargo build
```

## Key errors

1. **`link.exe: extra operand`** → MSYS `/usr/bin/link.exe` on PATH. Fix: switch to GNU target.
2. **`dlltool.exe: CreateProcess`** → mingw-w64 not installed.
3. **`BEGIN failed--compilation aborted`** → vendored openssl + MSYS Perl broken. Fix: pre-built OpenSSL.
4. **`feature 'rustls-tls' doesn't exist`** → use `__rustls-tls` (double underscore, v0.30 API).
5. **`expected Utf8Bytes, found String`** → `.into()` on Message::Text, `.as_str()` for connect_async.
