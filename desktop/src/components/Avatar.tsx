import { useEffect, useState } from 'react'

const palette = [
  '#697b95', '#7d8d71', '#9a7669', '#7c7397',
  '#6f8d8c', '#917a61', '#6d7f6f', '#8b6f7d',
]

function indexFor(value: string) {
  let hash = 0
  for (let i = 0; i < value.length; i += 1) hash = (hash * 31 + value.charCodeAt(i)) >>> 0
  return hash % palette.length
}

export default function Avatar({
  name,
  id,
  src,
  size = 44,
}: {
  name?: string | null
  id?: string | null
  src?: string | null
  size?: number
}) {
  const label = (name || id || '?').trim()
  const glyph = Array.from(label)[0] || '?'
  const [failed, setFailed] = useState(false)

  useEffect(() => setFailed(false), [src])

  return (
    <div
      className="avatar"
      style={{
        width: size,
        height: size,
        minWidth: size,
        background: palette[indexFor(id || label)],
        fontSize: Math.max(12, Math.round(size * 0.36)),
      }}
      title={label}
    >
      {src && !failed ? (
        <img
          className="avatar-image"
          src={src}
          alt={label}
          loading="lazy"
          onError={() => setFailed(true)}
        />
      ) : glyph}
    </div>
  )
}
