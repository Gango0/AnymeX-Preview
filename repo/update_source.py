import io
import json
import logging
import os
import plistlib
import re
import requests
import zipfile
from datetime import datetime
from typing import Dict, List, Optional, Any, TypedDict, Tuple

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

MAX_RECENT_ENTRIES = 5
REQUEST_TIMEOUT = 30


class ReleaseAsset(TypedDict):
    browser_download_url: str
    name: str
    size: int


class GitHubRelease(TypedDict):
    tag_name: str
    published_at: str
    body: str
    assets: List[ReleaseAsset]


class VersionEntry(TypedDict, total=False):
    version: str
    buildVersion: str
    date: str
    localizedDescription: str
    downloadURL: Optional[str]
    size: Optional[int]
    minOSVersion: str


class NewsEntry(TypedDict):
    appID: str
    title: str
    identifier: str
    caption: str
    date: str
    tintColor: str
    imageURL: str
    notify: bool
    url: str


class AppData(TypedDict):
    apps: List[Dict[str, Any]]
    news: List[NewsEntry]


class AppConfig(TypedDict):
    repo_url: str
    json_file: str
    app_id: str
    app_name: str
    caption: str
    tint_colour: str
    image_url: str


def load_config(config_path: str) -> AppConfig:
    try:
        with open(config_path, "r") as config_file:
            config_data = json.load(config_file)

        required_fields = [
            "repo_url",
            "json_file",
            "app_id",
            "app_name",
            "caption",
            "tint_colour",
            "image_url",
        ]
        missing_fields = [
            field for field in required_fields if field not in config_data
        ]

        if missing_fields:
            raise ValueError(
                f"Missing required configuration fields: {', '.join(missing_fields)}"
            )

        return {field: config_data[field] for field in required_fields}  # type: ignore[return-value]

    except FileNotFoundError:
        logging.exception(f"Configuration file not found at {config_path}")
        raise
    except json.JSONDecodeError as e:
        logging.exception(f"Invalid JSON in configuration file: {e}")
        raise
    except ValueError as e:
        logging.exception(str(e))
        raise


class HttpRangeReader(io.RawIOBase):
    def __init__(self, url: str, session: requests.Session) -> None:
        self.url = url
        self.session = session
        self.pos = 0

        response = session.head(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        if response.headers.get("Accept-Ranges", "").lower() != "bytes":
            raise OSError(f"Server does not advertise range support for {url}")

        self.size = int(response.headers["Content-Length"])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.size + offset
        else:
            raise ValueError(f"Invalid whence value: {whence}")

        return self.pos

    def readinto(self, buffer) -> int:  # type: ignore[no-untyped-def]
        if not len(buffer) or self.pos >= self.size:
            return 0

        end = min(self.pos + len(buffer), self.size) - 1
        response = self.session.get(
            self.url,
            headers={"Range": f"bytes={self.pos}-{end}"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        data = response.content
        buffer[: len(data)] = data
        self.pos += len(data)

        return len(data)


def read_ipa_metadata(
    download_url: str, session: requests.Session
) -> Dict[str, str]:
    try:
        reader = HttpRangeReader(download_url, session)

        with zipfile.ZipFile(io.BufferedReader(reader, buffer_size=256 * 1024)) as ipa:
            plist_path = next(
                name
                for name in ipa.namelist()
                if name.count("/") == 2 and name.endswith(".app/Info.plist")
            )
            info_plist = plistlib.loads(ipa.read(plist_path))
    except (requests.RequestException, OSError, zipfile.BadZipFile, StopIteration,
            plistlib.InvalidFileException, ValueError, KeyError) as e:
        logging.warning(f"Could not read bundle metadata from {download_url}: {e}")
        return {}

    metadata: Dict[str, str] = {}

    build_version = info_plist.get("CFBundleVersion")
    if build_version:
        metadata["buildVersion"] = str(build_version)

    min_os_version = info_plist.get("MinimumOSVersion")
    if min_os_version:
        metadata["minOSVersion"] = str(min_os_version)

    logging.info(f"Read {metadata} from {download_url.rsplit('/', 1)[-1]}")
    return metadata


def fetch_all_releases(repo_url: str, session: requests.Session) -> List[GitHubRelease]:
    api_url: str = f"https://api.github.com/repos/{repo_url}/releases"
    headers: Dict[str, str] = {"Accept": "application/vnd.github+json"}
    github_token = os.getenv("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    releases: List[GitHubRelease] = []
    page = 1

    while True:
        try:
            response = session.get(
                api_url,
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            logging.exception(f"Failed to fetch releases from {api_url}: {e}")
            raise

        page_releases: List[GitHubRelease] = response.json()
        releases.extend(page_releases)

        if len(page_releases) < 100:
            break

        page += 1

    sorted_releases = sorted(releases, key=lambda x: x["published_at"], reverse=False)

    logging.info(f"Fetched {len(sorted_releases)} releases from {repo_url}")
    return sorted_releases


def find_latest_installable_release(releases: List[GitHubRelease]) -> GitHubRelease:
    sorted_releases = sorted(releases, key=lambda x: x["published_at"], reverse=True)

    for release in sorted_releases:
        if any(asset["name"].endswith(".ipa") for asset in release["assets"]):
            logging.info(f"Latest installable release: {release['tag_name']}")
            return release

    raise ValueError("No installable releases with IPA assets found")


def format_description(description: str) -> str:
    formatted = re.sub(r"<[^>]+>", "", description)
    formatted = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", formatted)
    formatted = re.sub(
        r"\[([^\]]*)\]\([^)]*\)",
        lambda match: match.group(1) if re.search(r"\w", match.group(1)) else "",
        formatted,
    )
    formatted = re.sub(r"#{1,6}\s?", "", formatted)
    formatted = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", formatted)
    formatted = formatted.replace("**", "").replace("`", '"')
    formatted = re.sub(r"(?m)^[ \t]+$", "", formatted)
    formatted = re.sub(r"\n{3,}", "\n\n", formatted).strip()

    return formatted


def find_download_url_and_size(
    release: GitHubRelease,
) -> Tuple[Optional[str], Optional[int]]:
    for asset in release["assets"]:
        if asset["name"].endswith(".ipa"):
            return asset["browser_download_url"], asset["size"]

    logging.warning(f"No IPA file found for release {release['tag_name']}")
    return None, None


def normalize_version(version: str) -> str:
    version = version.lstrip("v")

    match = re.search(r"(\d+\.\d+\.\d+)", version)
    if match:
        return match.group(1)
    return version


def process_versions(versions_data: List[VersionEntry]) -> List[VersionEntry]:
    version_dict: Dict[str, VersionEntry] = {}

    for version in versions_data:
        current_date = datetime.fromisoformat(version["date"].replace("Z", "+00:00"))
        version_key = version["downloadURL"] or version["version"]

        if version_key in version_dict:
            existing_date = datetime.fromisoformat(
                version_dict[version_key]["date"].replace("Z", "+00:00")
            )

            if current_date > existing_date:
                version_dict[version_key] = version
        else:
            version_dict[version_key] = version

    result = list(version_dict.values())
    logging.info(
        f"Processed {len(versions_data)} versions, kept {len(result)} unique builds"
    )

    return result


def purge_old_news(data: AppData, max_entries: int = MAX_RECENT_ENTRIES) -> None:
    if "news" not in data or not isinstance(data["news"], list):
        return

    sorted_news = sorted(
        data["news"],
        key=lambda news_entry: news_entry.get("date", ""),
        reverse=True,
    )
    data["news"] = sorted_news[:max_entries]
    logging.info(f"Purged news from {len(sorted_news)} to {len(data['news'])} entries")


def collect_known_metadata(app: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    known: Dict[str, Dict[str, str]] = {}

    for version in app.get("versions", []):
        download_url = version.get("downloadURL")
        if not download_url:
            continue

        metadata = {
            key: version[key]
            for key in ("buildVersion", "minOSVersion")
            if version.get(key)
        }
        if metadata:
            known[download_url] = metadata

    return known


def update_json_file(
    config: AppConfig,
    json_file: str,
    fetched_data_all: List[GitHubRelease],
    fetched_data_latest_installable: GitHubRelease,
    session: requests.Session,
) -> None:
    try:
        with open(json_file, "r") as file:
            data: AppData = json.load(file)
    except FileNotFoundError:
        logging.exception(f"JSON file not found at {json_file}")
        raise
    except json.JSONDecodeError as e:
        logging.exception(f"Invalid JSON in {json_file}: {e}")
        raise

    if "apps" not in data or not data["apps"]:
        raise ValueError("Invalid JSON structure: 'apps' array is missing or empty")

    app = data["apps"][0]
    known_metadata = collect_known_metadata(app)

    releases = []

    for release in fetched_data_all:
        full_version = release["tag_name"].lstrip("v")
        version_match = re.search(r"(\d+\.\d+\.\d+)", full_version)

        if not version_match:
            logging.warning(
                f"Skipping release with invalid version format: {release['tag_name']}"
            )
            continue

        download_url, size = find_download_url_and_size(release)

        if not download_url:
            logging.warning(
                f"Skipping release {release['tag_name']} - no IPA file found"
            )
            continue

        version_entry: VersionEntry = {
            "version": normalize_version(full_version),
            "date": release["published_at"],
            "localizedDescription": format_description(release["body"]),
            "downloadURL": download_url,
            "size": size,
        }

        releases.append(version_entry)

    recent_versions = sorted(
        process_versions(releases), key=lambda x: x.get("date", ""), reverse=True
    )[:MAX_RECENT_ENTRIES]

    for version_entry in recent_versions:
        download_url = version_entry["downloadURL"]
        metadata = known_metadata.get(download_url) or read_ipa_metadata(
            download_url, session
        )
        version_entry.update(metadata)

    app["versions"] = recent_versions

    latest_version = fetched_data_latest_installable["tag_name"].lstrip("v")
    tag = fetched_data_latest_installable["tag_name"]
    version_match = re.search(r"(\d+\.\d+\.\d+)", latest_version)

    if not version_match:
        raise ValueError(f"Invalid version format for latest release: {latest_version}")

    app["version"] = normalize_version(latest_version)
    app["versionDate"] = fetched_data_latest_installable["published_at"]
    app["versionDescription"] = format_description(
        fetched_data_latest_installable["body"]
    )

    download_url, size = find_download_url_and_size(fetched_data_latest_installable)
    app["downloadURL"] = download_url
    app["size"] = size

    latest_metadata = next(
        (v for v in recent_versions if v["downloadURL"] == download_url), {}
    )
    for key in ("buildVersion", "minOSVersion"):
        if latest_metadata.get(key):
            app[key] = latest_metadata[key]
        else:
            app.pop(key, None)
            logging.warning(f"No {key} available for {tag}")

    if "news" not in data:
        data["news"] = []

    news_identifier = f"release-{latest_version}"
    if not any(item["identifier"] == news_identifier for item in data["news"]):
        try:
            formatted_date = datetime.strptime(
                fetched_data_latest_installable["published_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).strftime("%d %b")
        except ValueError as e:
            logging.exception(f"Error parsing date: {e}")
            formatted_date = "Unknown"

        news_entry: NewsEntry = {
            "appID": config["app_id"],
            "title": f"{latest_version} - {formatted_date}",
            "identifier": news_identifier,
            "caption": config["caption"],
            "date": fetched_data_latest_installable["published_at"],
            "tintColor": config["tint_colour"],
            "imageURL": config["image_url"],
            "notify": True,
            "url": f"https://github.com/{config['repo_url']}/releases/tag/{tag}",
        }
        data["news"].append(news_entry)
        logging.info(f"Added news entry for version {latest_version}")

    purge_old_news(data)

    try:
        with open(json_file, "w") as file:
            json.dump(data, file, indent=2)
        logging.info(f"Successfully updated {json_file}")
    except IOError as e:
        logging.exception(f"Failed to write to {json_file}: {e}")
        raise


def main() -> None:
    try:
        logging.info("Starting release update process")

        config = load_config("repo/config.json")

        with requests.Session() as session:
            fetched_data_all = fetch_all_releases(config["repo_url"], session)
            fetched_data_latest_installable = find_latest_installable_release(
                fetched_data_all
            )
            update_json_file(
                config,
                config["json_file"],
                fetched_data_all,
                fetched_data_latest_installable,
                session,
            )

        logging.info(f"Successfully updated {config['json_file']} with latest releases")

    except Exception as e:
        logging.exception(f"Error updating releases: {e}")
        raise


if __name__ == "__main__":
    main()
