import { useEffect, useRef, useState } from 'react'

// Hover-only overflow marquee: 35 px/s, 40 px gap, and a five-second cycle hold.
export default function FollowName({ text, active, onHover, hint = false }: {
  text: string
  active: boolean
  onHover(hovered: boolean): void
  hint?: boolean
}) {
  const wrapperRef = useRef<HTMLDivElement>(null)
  const trackRef = useRef<HTMLSpanElement>(null)
  const [running, setRunning] = useState(false)

  useEffect(() => {
    const wrapper = wrapperRef.current
    const track = trackRef.current
    if (!wrapper || !track) return
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)')
    let animation: Animation | undefined
    let disposed = false
    const measure = () => {
      if (disposed) return
      animation?.cancel()
      setRunning(false)
      const width = track.firstElementChild?.getBoundingClientRect().width ?? 0
      if (!active || preference.matches || width <= wrapper.clientWidth) return
      const distance = width + 40
      const travel = distance / 35 * 1000
      setRunning(true)
      animation = track.animate([
          { transform: 'translateX(0)', offset: 0 },
          { transform: `translateX(-${distance}px)`, offset: travel / (travel + 5000) },
          { transform: `translateX(-${distance}px)`, offset: 1 },
      ], { duration: travel + 5000, iterations: Infinity, easing: 'linear' })
    }
    const observer = new ResizeObserver(measure)
    observer.observe(wrapper)
    preference.addEventListener('change', measure)
    void document.fonts.ready.then(() => { if (wrapper.isConnected) measure() })
    return () => {
      disposed = true
      observer.disconnect()
      preference.removeEventListener('change', measure)
      animation?.cancel()
    }
  }, [text, active])

  return <div ref={wrapperRef} className={`follow-name${hint ? ' follow-preparing' : ''}${active && running ? ' scrolling' : ''}`} role={hint ? 'status' : 'heading'} aria-level={hint ? undefined : 2} title={text} onMouseEnter={() => onHover(true)} onMouseLeave={() => onHover(false)}>
    <span className="follow-name-static">{text}</span>
    <span ref={trackRef} className="follow-name-track" aria-hidden="true"><span>{text}</span><span>{text}</span></span>
  </div>
}
