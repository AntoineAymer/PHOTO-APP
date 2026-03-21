"""Native Apple Photos bridge via PyObjC — direct library access, no export."""

import os
import threading
import time
from pathlib import Path

import objc
import Photos
from Foundation import NSRunLoop, NSDate

# Directories
WORK_DIR = os.path.expanduser("~/.photoframe")
PHOTOS_CACHE_DIR = os.path.join(WORK_DIR, "photos_cache")
os.makedirs(PHOTOS_CACHE_DIR, exist_ok=True)


def request_photos_access() -> bool:
    """Request access to Apple Photos. Returns True if granted."""
    status = Photos.PHPhotoLibrary.authorizationStatusForAccessLevel_(
        Photos.PHAccessLevelReadWrite
    )

    if status == 3:  # authorized
        return True

    if status == 0:  # not determined — request
        result = {"granted": False, "done": False}

        def callback(new_status):
            result["granted"] = new_status == 3
            result["done"] = True

        Photos.PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_(
            Photos.PHAccessLevelReadWrite, callback
        )

        # Wait for user to respond to dialog
        timeout = 120  # 2 minutes
        start = time.time()
        while not result["done"] and time.time() - start < timeout:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
        return result["granted"]

    # status 2 (denied) or 1 (restricted)
    return False


def list_albums() -> list[dict]:
    """List all user albums from Apple Photos."""
    if not request_photos_access():
        raise RuntimeError(
            "Photos access denied. Go to System Settings > Privacy & Security > Photos "
            "and allow access for Terminal (or your Python app)."
        )

    albums = []

    # User albums
    user_albums = Photos.PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        Photos.PHAssetCollectionTypeAlbum,
        Photos.PHAssetCollectionSubtypeAny,
        None
    )
    for i in range(user_albums.count()):
        album = user_albums.objectAtIndex_(i)
        assets = Photos.PHAsset.fetchAssetsInAssetCollection_options_(album, None)
        albums.append({
            "name": str(album.localizedTitle() or "Untitled"),
            "count": assets.count(),
            "id": str(album.localIdentifier()),
            "type": "album",
        })

    # Smart albums (Favorites, Recently Added, etc.)
    smart_albums = Photos.PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        Photos.PHAssetCollectionTypeSmartAlbum,
        Photos.PHAssetCollectionSubtypeAny,
        None
    )
    for i in range(smart_albums.count()):
        album = smart_albums.objectAtIndex_(i)
        assets = Photos.PHAsset.fetchAssetsInAssetCollection_options_(album, None)
        count = assets.count()
        if count > 0:  # Skip empty smart albums
            albums.append({
                "name": str(album.localizedTitle() or "Untitled"),
                "count": count,
                "id": str(album.localIdentifier()),
                "type": "smart_album",
            })

    return albums


def get_album_assets(album_name: str) -> list[dict]:
    """Get all assets (photos/videos) from a named album."""
    if not request_photos_access():
        raise RuntimeError("Photos access denied")

    # Find the album
    album = _find_album_by_name(album_name)
    if not album:
        raise RuntimeError(f"Album '{album_name}' not found")

    # Fetch assets
    fetch_options = Photos.PHFetchOptions.alloc().init()
    fetch_options.setSortDescriptors_([
        objc.lookUpClass("NSSortDescriptor").sortDescriptorWithKey_ascending_(
            "creationDate", True
        )
    ])

    assets_result = Photos.PHAsset.fetchAssetsInAssetCollection_options_(
        album, fetch_options
    )

    assets = []
    for i in range(assets_result.count()):
        asset = assets_result.objectAtIndex_(i)
        asset_info = {
            "id": str(asset.localIdentifier()),
            "filename": str(asset.valueForKey_("filename") or f"photo_{i}"),
            "media_type": "video" if asset.mediaType() == Photos.PHAssetMediaTypeVideo else "image",
            "width": asset.pixelWidth(),
            "height": asset.pixelHeight(),
            "creation_date": str(asset.creationDate()) if asset.creationDate() else None,
            "duration": asset.duration() if asset.mediaType() == Photos.PHAssetMediaTypeVideo else None,
        }
        assets.append(asset_info)

    return assets


def export_asset_to_cache(asset_id: str) -> str | None:
    """Export a single asset from Photos library to cache directory.
    Returns the path to the exported file."""
    if not request_photos_access():
        return None

    # Fetch the asset by ID
    result = Photos.PHAsset.fetchAssetsWithLocalIdentifiers_options_(
        [asset_id], None
    )
    if result.count() == 0:
        return None

    asset = result.objectAtIndex_(0)
    filename = str(asset.valueForKey_("filename") or f"asset_{asset_id[:8]}")

    # Determine output path
    safe_name = filename.replace("/", "_")
    out_path = os.path.join(PHOTOS_CACHE_DIR, safe_name)

    if os.path.exists(out_path):
        return out_path

    # Request the image/video data
    if asset.mediaType() == Photos.PHAssetMediaTypeImage:
        return _export_image(asset, out_path)
    elif asset.mediaType() == Photos.PHAssetMediaTypeVideo:
        return _export_video(asset, out_path)

    return None


def export_album_to_cache(album_name: str) -> list[str]:
    """Export all assets from an album to cache. Returns list of file paths."""
    if not request_photos_access():
        raise RuntimeError("Photos access denied")

    album = _find_album_by_name(album_name)
    if not album:
        raise RuntimeError(f"Album '{album_name}' not found")

    fetch_options = Photos.PHFetchOptions.alloc().init()
    fetch_options.setSortDescriptors_([
        objc.lookUpClass("NSSortDescriptor").sortDescriptorWithKey_ascending_(
            "creationDate", True
        )
    ])

    assets_result = Photos.PHAsset.fetchAssetsInAssetCollection_options_(
        album, fetch_options
    )

    exported_paths = []
    image_manager = Photos.PHImageManager.defaultManager()

    for i in range(assets_result.count()):
        asset = assets_result.objectAtIndex_(i)
        asset_id = str(asset.localIdentifier())
        filename = str(asset.valueForKey_("filename") or f"photo_{i}")
        safe_name = filename.replace("/", "_")
        out_path = os.path.join(PHOTOS_CACHE_DIR, safe_name)

        if os.path.exists(out_path):
            exported_paths.append(out_path)
            continue

        if asset.mediaType() == Photos.PHAssetMediaTypeImage:
            path = _export_image(asset, out_path)
        elif asset.mediaType() == Photos.PHAssetMediaTypeVideo:
            path = _export_video(asset, out_path)
        else:
            continue

        if path:
            exported_paths.append(path)

    return exported_paths


def _find_album_by_name(name: str):
    """Find an album by name (user albums first, then smart albums)."""
    # User albums
    user_albums = Photos.PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        Photos.PHAssetCollectionTypeAlbum,
        Photos.PHAssetCollectionSubtypeAny,
        None
    )
    for i in range(user_albums.count()):
        album = user_albums.objectAtIndex_(i)
        if str(album.localizedTitle()) == name:
            return album

    # Smart albums
    smart_albums = Photos.PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        Photos.PHAssetCollectionTypeSmartAlbum,
        Photos.PHAssetCollectionSubtypeAny,
        None
    )
    for i in range(smart_albums.count()):
        album = smart_albums.objectAtIndex_(i)
        if str(album.localizedTitle()) == name:
            return album

    return None


def _export_image(asset, out_path: str) -> str | None:
    """Export an image asset to a file."""
    manager = Photos.PHImageManager.defaultManager()

    options = Photos.PHImageRequestOptions.alloc().init()
    options.setSynchronous_(True)
    options.setDeliveryMode_(Photos.PHImageRequestOptionsDeliveryModeHighQualityFormat)
    options.setNetworkAccessAllowed_(True)  # Download from iCloud if needed

    result_holder = {"data": None, "done": False}

    def handler(imageData, dataUTI, orientation, info):
        if imageData:
            result_holder["data"] = imageData
        result_holder["done"] = True

    manager.requestImageDataAndOrientationForAsset_options_resultHandler_(
        asset, options, handler
    )

    # Wait for sync completion
    timeout = 60
    start = time.time()
    while not result_holder["done"] and time.time() - start < timeout:
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.1)
        )

    if result_holder["data"]:
        data = result_holder["data"]
        # Write to file
        with open(out_path, "wb") as f:
            f.write(data.bytes().tobytes())
        return out_path

    return None


def _export_video(asset, out_path: str) -> str | None:
    """Export a video asset to a file."""
    manager = Photos.PHImageManager.defaultManager()

    options = Photos.PHVideoRequestOptions.alloc().init()
    options.setNetworkAccessAllowed_(True)
    options.setDeliveryMode_(Photos.PHVideoRequestOptionsDeliveryModeHighQualityFormat)

    result_holder = {"url": None, "done": False}

    def handler(avAsset, audioMix, info):
        if avAsset and hasattr(avAsset, "URL"):
            result_holder["url"] = str(avAsset.URL().path())
        result_holder["done"] = True

    manager.requestAVAssetForVideo_options_resultHandler_(
        asset, options, handler
    )

    timeout = 120
    start = time.time()
    while not result_holder["done"] and time.time() - start < timeout:
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.1)
        )

    if result_holder["url"]:
        import shutil
        shutil.copy2(result_holder["url"], out_path)
        return out_path

    return None


# ═══ Quick test ═══
if __name__ == "__main__":
    print("Requesting Photos access...")
    if request_photos_access():
        print("Access granted!")
        albums = list_albums()
        for a in albums:
            marker = " <<<" if a["name"] == "Flo" else ""
            print(f"  {a['type']:12s} | {a['name']:30s} | {a['count']} items{marker}")

        # Try to find Flo
        flo_assets = get_album_assets("Flo")
        print(f"\nFlo album: {len(flo_assets)} assets")
        for a in flo_assets[:5]:
            print(f"  {a['filename']} ({a['media_type']}, {a['width']}x{a['height']})")
    else:
        print("Access denied! Go to System Settings > Privacy > Photos")
