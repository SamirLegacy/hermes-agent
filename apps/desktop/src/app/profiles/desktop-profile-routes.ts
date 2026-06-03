export const DESKTOP_PROFILE_REMOTE_URLS: Readonly<Record<string, string>> = {
  default: 'http://127.0.0.1:49220',
  bountyloop: 'http://127.0.0.1:49221',
  marketingscout: 'http://127.0.0.1:49222',
  newsletteros: 'http://127.0.0.1:49223',
  productfunnelcreator: 'http://127.0.0.1:49224',
  revenuescout: 'http://127.0.0.1:49225',
  trendscout: 'http://127.0.0.1:49226'
}

export function desktopProfileRemoteUrl(profileName: string): string | null {
  return DESKTOP_PROFILE_REMOTE_URLS[profileName] ?? null
}

export function normalizeDesktopRemoteUrl(value: null | string | undefined): string {
  const raw = String(value || '').trim()

  if (!raw) {
    return ''
  }

  try {
    const url = new URL(raw)

    url.hash = ''
    url.search = ''
    url.pathname = url.pathname.replace(/\/+$/, '')

    return url.toString().replace(/\/+$/, '')
  } catch {
    return raw.replace(/\/+$/, '')
  }
}

export function desktopProfileForRemoteUrl(remoteUrl: null | string | undefined): string | null {
  const normalized = normalizeDesktopRemoteUrl(remoteUrl)

  for (const [profileName, url] of Object.entries(DESKTOP_PROFILE_REMOTE_URLS)) {
    if (normalizeDesktopRemoteUrl(url) === normalized) {
      return profileName
    }
  }

  return null
}
