/**
 * Landing page behaviour.
 *
 * Two things only: run the hero's draw-and-stack sequence once when the page is
 * ready, and reveal sections as they come into view. Everything else on the page
 * is static — the boldness is spent on the ledger, so nothing else moves.
 */

import './landing.css'
import '@fontsource/archivo/600.css'
import '@fontsource/archivo/700.css'
import '@fontsource/archivo/800.css'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'

const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches

/** The hero sequence: the blank scribes itself, then its calls land in order. */
function runLedger() {
  const ledger = document.getElementById('ledger')
  if (!ledger) return
  // The CSS holds the end state; `.run` releases it. With reduced motion the
  // same class applies and the animations are disabled in the stylesheet, so
  // the finished state shows immediately rather than being skipped entirely.
  ledger.classList.add('run')
}

/** Sections fade up once, the first time they are seen. */
function revealOnScroll() {
  const targets = Array.from(document.querySelectorAll<HTMLElement>('.rise'))
  if (reduced || !('IntersectionObserver' in window)) {
    targets.forEach((el) => el.classList.add('shown'))
    return
  }
  const io = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue
        entry.target.classList.add('shown')
        io.unobserve(entry.target)
      }
    },
    { rootMargin: '0px 0px -12% 0px', threshold: 0.1 },
  )
  targets.forEach((el) => io.observe(el))
}

function start() {
  runLedger()
  revealOnScroll()
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start, { once: true })
} else {
  start()
}
