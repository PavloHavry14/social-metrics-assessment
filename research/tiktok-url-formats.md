# TikTok & Instagram URL Formats Research

## TikTok URL Patterns

### 1. Canonical (Full) URL Format

The canonical TikTok video URL contains the video ID and is the "ground truth" format:

```
https://www.tiktok.com/@{username}/video/{video_id}
```

Example:
```
https://www.tiktok.com/@ckenthusiast/video/7252269798455840027
```

Components:
- `@{username}` -- the creator's handle (can change over time due to account reassignment)
- `{video_id}` -- a 19-digit numeric snowflake ID (e.g., `7252269798455840027`)
- The video ID encodes a timestamp (similar to Twitter snowflake IDs)

Variations of the canonical URL:
```
https://www.tiktok.com/@username/video/1234567890123456789
https://tiktok.com/@username/video/1234567890123456789        (no www)
https://m.tiktok.com/@username/video/1234567890123456789      (mobile web)
```

### 2. Share URL Formats (Shortened)

These are generated when a user taps "Copy Link" inside the TikTok app:

```
https://vm.tiktok.com/{shortcode}
https://vt.tiktok.com/{shortcode}
```

Example:
```
https://vm.tiktok.com/ZMeKQ5kRd/
https://vt.tiktok.com/ZSRx9Abcd/
```

**CRITICAL: Share URLs do NOT contain the video ID.** The `{shortcode}` is an opaque alphanumeric token (typically 9-11 characters, base62-like). There is NO way to extract the video ID from the shortcode without making an HTTP request.

### 3. Regional Variations of Share URLs

Different subdomains are used based on the user's region and sharing context:

| Subdomain | Typical Region/Context |
|-----------|----------------------|
| `vm.tiktok.com` | Global default (Americas, Europe) |
| `vt.tiktok.com` | Asia / Southeast Asia regions |
| `m.tiktok.com` | Mobile web (when user manually copies from browser) |

All of these are functionally equivalent short-link redirectors.

### 4. How Share URLs Redirect

Share URLs perform a **302 redirect** to the canonical URL:

```
GET https://vm.tiktok.com/ZMeKQ5kRd/
=> 302 Found
=> Location: https://www.tiktok.com/@username/video/7252269798455840027?is_from_webapp=1&sender_device=pc
```

Key observations:
- The redirect target is the canonical URL with tracking query parameters appended
- Query params include: `is_from_webapp`, `sender_device`, `sender_web_id`, `tt_from`, etc.
- The video ID can ONLY be obtained by following this redirect (or doing a HEAD request)
- TikTok may also return a 301 in some cases
- The redirect may go through multiple hops (e.g., short URL -> m.tiktok.com -> www.tiktok.com)

### 5. Tracking Parameters

TikTok appends extensive tracking data to share URLs:

```
?is_from_webapp=1&sender_device=pc&web_id=1234567890
&tt_from=copy&sender_web_id=1234567890
```

The `ttclid` parameter is particularly notable -- it attributes clicks, sales, and shares back to the sharing user's account.

### 6. Profile URL Format

```
https://www.tiktok.com/@{username}
```

No numeric user ID is exposed in profile URLs.

### 7. Extracting Video ID Without HTTP Requests

**This is NOT possible for share URLs.** The shortcode is opaque and not a transformation of the video ID.

For canonical URLs, the video ID can be extracted with a simple regex:
```
/tiktok\.com\/@[\w.]+\/video\/(\d+)/
```

This is the ONLY pattern where video ID extraction is possible without network requests.

### 8. Other TikTok URL Patterns

```
# Sound/music pages
https://www.tiktok.com/music/{slug}-{music_id}

# Hashtag/challenge pages
https://www.tiktok.com/tag/{hashtag}

# Effect pages
https://www.tiktok.com/sticker/{effect_name}-{effect_id}

# Live streams
https://www.tiktok.com/@{username}/live

# Photo posts (newer format)
https://www.tiktok.com/@{username}/photo/{photo_id}
```

---

## Instagram URL Patterns

### 1. Post URLs

```
https://www.instagram.com/p/{shortcode}/
https://instagram.com/p/{shortcode}/
```

The `{shortcode}` is a base64-like alphanumeric identifier (e.g., `CzDoL9Vh789`), typically 11 characters. Unlike TikTok share URLs, the Instagram shortcode IS the canonical identifier -- no redirect resolution needed.

### 2. Reel URLs

```
https://www.instagram.com/reel/{shortcode}/
https://www.instagram.com/{username}/reel/{shortcode}/
```

Example:
```
https://www.instagram.com/reel/DOvzTywjPGN/
https://www.instagram.com/username/reel/DOvzTywjPGN/
```

Both formats use the same shortcode and refer to the same content.

### 3. Story URLs

```
https://www.instagram.com/stories/{username}/{story_media_id}/
```

Stories are ephemeral (24 hours) and identified by a numeric media ID.

### 4. IGTV / TV URLs (legacy)

```
https://www.instagram.com/tv/{shortcode}/
```

Uses the same shortcode system as posts.

### 5. Profile URLs

```
https://www.instagram.com/{username}/
```

### 6. Instagram Share/Shortened URLs

Instagram does NOT use a separate short-link domain like TikTok. The `/p/{shortcode}/` format is already short. However, there is an older format:

```
https://instagr.am/p/{shortcode}/
```

### 7. Instagram Numeric Media ID vs Shortcode

Instagram shortcodes can be converted to numeric media IDs algorithmically (base64 decode). This means you CAN extract the numeric ID from the URL without an HTTP request -- the opposite situation from TikTok share URLs.

---

## Summary: Key Implications for Challenge 01

| Platform | URL Type | Contains ID? | Needs HTTP to resolve? |
|----------|----------|-------------|----------------------|
| TikTok | Canonical (`/@user/video/{id}`) | YES (numeric) | No |
| TikTok | Share (`vm.tiktok.com/{code}`) | NO | YES (302 redirect) |
| TikTok | Share (`vt.tiktok.com/{code}`) | NO | YES (302 redirect) |
| Instagram | Post/Reel (`/p/{shortcode}`) | YES (shortcode) | No |
| Instagram | Reel (`/reel/{shortcode}`) | YES (shortcode) | No |
| Instagram | Story (`/stories/{user}/{id}`) | YES (numeric) | No |

**The critical challenge**: When reconciling metrics from two API providers, one provider may report the share URL (`vm.tiktok.com/...`) while the other reports the canonical URL (`tiktok.com/@user/video/...`). These cannot be matched without resolving the share URL via an HTTP request to follow the 302 redirect. This is the core difficulty of share URL resolution in Challenge 01.
