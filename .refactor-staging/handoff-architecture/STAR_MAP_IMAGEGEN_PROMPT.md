# Image-generation handoff: Exoplanet Hunt spatial dashboard

Create a high-fidelity desktop UI concept image for an interactive web application called **EXOHUNT // LOCAL STELLAR SURVEY**. This image is a visual baseline that will later be implemented as a real HTML/CSS/JavaScript or WebGL interface, so prioritize a coherent, buildable product layout over abstract science-fiction art.

Show a sleek 16:9 mission-control dashboard centered on a beautiful interactive **3D map of stars analyzed spatially relative to Earth**. Place the Sun/Earth system at the origin as a small warm-white point with a subtle cyan locator ring. Surround it with scientifically meaningful translucent distance shells labeled **10 pc, 25 pc, 50 pc, 100 pc, 150 pc**, plus restrained coordinate axes and a faint ecliptic or galactic reference plane. Plot hundreds of stars at varied three-dimensional positions. Depth, scale, and perspective must be immediately understandable.

Use a clear visual status language:

- dim cool-gray points: eligible but not searched;
- bright cyan points: analyzed stars with no surviving signal;
- amber points with a thin orbital ring: known planet rediscoveries;
- violet points with a pulsing diamond or double ring: known TCE rediscoveries;
- muted red hollow markers: vetted false positives;
- brilliant white-green beacon with expanding rings: a future vetted new candidate;
- use the candidate state sparingly, as an example UI legend item rather than claiming a real discovery.

Show several subtle orbital rings around rediscovered systems. Use thin luminous connection lines only for the currently selected star and its projection to the coordinate plane; do not connect every star. Add tasteful density falloff and atmospheric depth without turning the map into a random nebula.

The currently selected system should be **TIC 260708537 — Known TCE Rediscovery**. Display a compact right-side inspection panel with clean data rows such as **Distance**, **TESS magnitude**, **Stellar radius**, **Observed sectors**, **Recovered period**, **Transit depth**, **Signal-to-noise**, **Catalog status**, and **Pixel-centroid result**. Include a small phase-folded transit sparkline and a miniature orbit diagram. Clearly label the status as **REDISCOVERED / NOT A NEW PLANET**.

Create a slim top command bar with the product title, current dataset **TESS Sector 105 + archival sectors**, search box for TIC/name, and compact live statistics: **Stars analyzed**, **Known planets recovered**, **TCEs rediscovered**, **Vetted false positives**, **New candidates**. Use believable placeholder counts or tokens that can later be bound to live data; do not make the numbers the visual centerpiece.

Create a left filter rail with polished toggles and controls for **Status**, **Distance**, **TESS sector**, **Stellar temperature**, **Stellar radius**, and **Minimum signal-to-noise**. Include buttons for **3D space**, **sky projection**, and **Earth view**. Along the bottom, show a compact campaign timeline/progress scrubber and a legend. Add small interaction hints for **drag to orbit**, **scroll to zoom**, **hover to inspect**, and **click to pin**.

Art direction: sophisticated modern aerospace data visualization, dark near-black and deep-navy background, restrained electric cyan, violet, amber, and white accents, subtle glass panels, fine grid lines, precise typography, excellent contrast, soft bloom only on important markers, realistic volumetric depth, crisp charts, premium product-design finish. Think contemporary scientific observatory software blended with a top-tier spatial analytics product. The interface should feel ambitious, credible, calm, and technically rigorous.

Avoid illegible microtext, clutter, excessive neon, generic spaceship controls, fantasy planets, giant decorative Earth imagery, lens-flare overload, random HUD circles, fake company logos, or a flat starfield with no spatial meaning. Do not show source code. Make every visible control look feasible to implement in HTML, CSS, WebGL/Three.js, and standard chart components.

Output: one ultra-detailed 3840×2160 desktop dashboard concept, straight-on screen view, no laptop frame, no people, no watermark. Typography and layout should remain legible enough to guide frontend implementation.

## Optional negative prompt

Unbuildable fantasy HUD, illegible text, excessive cyberpunk neon, cluttered panels, random decorative charts, inaccurate giant planets, spaceship cockpit, monitor mockup, laptop frame, people, watermark, logo, mobile layout, low contrast, flat star distribution, no distance scale, oversaturated nebula, lens flare, childish game UI.
