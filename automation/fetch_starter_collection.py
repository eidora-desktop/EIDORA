"""Fetch the exact starter asset for a release build; never used by the app."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.request
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from project.starter_collection import read_manifest
from project.starter_validation import sha256


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        if urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header('Authorization')
        return redirected


def open_release_asset(manifest):
    opener = urllib.request.build_opener(ReleaseRedirect())
    headers = {'User-Agent': 'EIDORA-release-build'}
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    url = manifest['url']
    if token:
        # Actions can read release assets even while the repository is private.
        headers.update(Authorization='Bearer ' + token, Accept='application/vnd.github+json')
        tag = url.split('/download/', 1)[1].split('/', 1)[0]
        request = urllib.request.Request('https://api.github.com/repos/eidora-desktop/EIDORA/releases/tags/' + tag, headers=headers)
        with opener.open(request, timeout=90) as response:
            release = json.load(response)
        assets = [asset for asset in release.get('assets', []) if asset['name'] == manifest['archive']]
        if len(assets) != 1:
            raise ValueError('Pinned starter asset is missing from its release.')
        url = assets[0]['url']
        if not url.startswith('https://api.github.com/repos/eidora-desktop/EIDORA/releases/assets/'):
            raise ValueError('Unexpected release asset API URL.')
        headers['Accept'] = 'application/octet-stream'
    return opener.open(urllib.request.Request(url, headers=headers), timeout=90)


def verify_archive(path, manifest, *, cached=False):
    size = path.stat().st_size
    digest = sha256(path)
    if size == manifest['archive_bytes'] and digest == manifest['sha256']:
        return
    source = 'Cached' if cached else 'Downloaded'
    action = ('Remove the cached archive before retrying.' if cached else
              'Compare the published archive and release manifest with the pinned manifest; '
              'publish future collection changes under a new version.')
    raise ValueError(
        f"{source} starter archive checksum mismatch for {manifest['archive']}. "
        f"Expected {manifest['archive_bytes']} bytes, SHA-256 {manifest['sha256']}; "
        f"received {size} bytes, SHA-256 {digest}. {action}"
    )


def fetch(root=ROOT):
    manifest_path = root/'src/project/resources/starter-collection.json'
    manifest = read_manifest(manifest_path)
    destination = root/'build/starter'/manifest['archive']
    if destination.exists():
        verify_archive(destination, manifest, cached=True)
        return destination
    url = manifest['url']
    if not url.startswith('https://github.com/eidora-desktop/EIDORA/releases/download/starter-met-v'):
        raise ValueError('Unexpected starter release URL.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary = destination.with_suffix('.download')
    try:
        with open_release_asset(manifest) as response, temporary.open('wb') as stream:
            shutil.copyfileobj(response,stream)
        verify_archive(temporary, manifest)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(fetch())
