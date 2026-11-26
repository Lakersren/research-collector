"""Public image paths used by the NVIDIA Technical Blog."""
from urllib.parse import urlsplit, urlunsplit


def image_candidates(url):
    parts = urlsplit(url)
    if (parts.scheme == "https" and parts.netloc == "developer-blogs.nvidia.com"
            and parts.path.startswith("/wp-content/uploads/")):
        yield urlunsplit(("https", "developer.nvidia.com", "/blog" + parts.path, parts.query, ""))
    yield url
