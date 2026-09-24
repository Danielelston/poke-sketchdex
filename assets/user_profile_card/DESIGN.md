---
name: Trainer Deck Activity
colors:
  surface: '#111317'
  surface-dim: '#111317'
  surface-bright: '#37393d'
  surface-container-lowest: '#0c0e12'
  surface-container-low: '#1a1c1f'
  surface-container: '#1e2023'
  surface-container-high: '#282a2e'
  surface-container-highest: '#333539'
  on-surface: '#e2e2e7'
  on-surface-variant: '#c6c5d7'
  inverse-surface: '#e2e2e7'
  inverse-on-surface: '#2e3034'
  outline: '#8f8fa0'
  outline-variant: '#454655'
  surface-tint: '#bec2ff'
  primary: '#bec2ff'
  on-primary: '#000da4'
  primary-container: '#5865f2'
  on-primary-container: '#fffdff'
  inverse-primary: '#3f4cda'
  secondary: '#66de8c'
  on-secondary: '#003919'
  secondary-container: '#23a55a'
  on-secondary-container: '#003115'
  tertiary: '#fbbc3c'
  on-tertiary: '#422d00'
  tertiary-container: '#9a6d00'
  on-tertiary-container: '#fffdff'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#e0e0ff'
  primary-fixed-dim: '#bec2ff'
  on-primary-fixed: '#000569'
  on-primary-fixed-variant: '#222fc2'
  secondary-fixed: '#83fba5'
  secondary-fixed-dim: '#66de8c'
  on-secondary-fixed: '#00210c'
  on-secondary-fixed-variant: '#005228'
  tertiary-fixed: '#ffdea8'
  tertiary-fixed-dim: '#fbbc3c'
  on-tertiary-fixed: '#271900'
  on-tertiary-fixed-variant: '#5e4200'
  background: '#111317'
  on-background: '#e2e2e7'
  surface-variant: '#333539'
typography:
  headline-lg:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '700'
    lineHeight: 32px
    letterSpacing: -0.015em
  headline-lg-mobile:
    fontFamily: Inter
    fontSize: 20px
    fontWeight: '700'
    lineHeight: 26px
    letterSpacing: -0.015em
  headline-md:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '700'
    lineHeight: 24px
    letterSpacing: -0.01em
  headline-sm:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '600'
    lineHeight: 22px
  body-lg:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: '400'
    lineHeight: 20px
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 18px
  body-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
  label-lg:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.02em
  label-md:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '700'
    lineHeight: 14px
    letterSpacing: 0.04em
  label-sm:
    fontFamily: Inter
    fontSize: 10px
    fontWeight: '800'
    lineHeight: 12px
    letterSpacing: 0.06em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 0.75rem
  margin: 1rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 0.75rem
  space-lg: 1rem
  space-xl: 1.5rem
---

## Brand & Style

This design system is tailored for an embedded Discord Activity mobile interface, bridging Discord’s modern, chat-centric dark gaming aesthetic with the tactile, data-rich energy of competitive Pokémon tracking. The target audience comprises mobile Discord gamers, raid groups, and competitive battle coordinators who require instant readability, effortless party management, and rich statistical breakdowns without leaving the Discord call.

The design movement combines **Modern Tactile Gaming** with **Discord Surface Hierarchy**:
- **Native Context:** Seamless integration into Discord's modal overlay shell, featuring the standard close affordance, server activity header banner, and bottom navigation.
- **Data Density & Legibility:** Clean information architecture utilizing distinct elemental color codes, progress meters, and segmented stats cards.
- **Tactile Gamification:** Subtle active-state compressions, glossy badge pills, and responsive slot interactions that feel directly lifted from handheld battle monitors.

## Colors

The palette directly honors Discord’s signature dark theme while incorporating high-chroma accents for core game statistics and elemental classifications.

### Surface & Neutral Architecture
- **Base Canvas (`#1E1F22`):** Primary background behind modals, bottom sheet underlays, and peripheral frames.
- **Activity Container (`#232428`):** Main view background, list containers, and empty party slot borders.
- **Card & Tile Surface (`#2B2D31`):** Default card background for Pokémon party members, trainer license plaques, and inventory cells.
- **Interactive Surface (`#313338`):** Hover states, segmented control tracks, and input field backgrounds.
- **Text & Foreground:** `#DBDEE1` (Primary text & icons), `#949BA4` (Subtext, unequipped stats, and captions), `#FFFFFF` (Headings, primary CTA text).

### Functional & Status Colors
- **Blurple Primary (`#5865F2`):** Core callouts, selected segments, active tabs, and confirmation actions.
- **Success / HP Full (`#23A55A`):** High-health indicators, positive nature stats, online state.
- **Warning / EXP Gold (`#F0B232`):** Experience meters, EV/IV highlights, mid-health warning.
- **Danger / Fainted (`#F23F43`):** Critical HP, negative nature debuffs, fainting indicators.

### Pokémon Elemental Type Accent Tokens
Type badges use high-vibrancy fills with contrasting dark or pure white typography:
- **Normal:** `#9FA19F`
- **Fire:** `#E65100` (Vibrant Deep Orange)
- **Water:** `#00B0FF` (Cyan Blue)
- **Grass:** `#43A047` (Vibrant Lime/Grass Green)
- **Electric:** `#FFD600` (Volt Yellow, text: `#1E1F22`)
- **Ice:** `#00E5FF`
- **Fighting:** `#C62828`
- **Poison:** `#9C27B0`
- **Ground:** `#A1887F`
- **Flying:** `#80D8FF`
- **Psychic:** `#FF4081`
- **Bug:** `#8BC34A`
- **Rock:** `#8D6E63`
- **Ghost:** `#4527A0` (Deep Indigo)
- **Dragon:** `#7C4DFF` (Royal Dragon Purple)
- **Dark:** `#424242`
- **Steel:** `#78909C`
- **Fairy:** `#FF80AB`

## Typography

The typography leverages **Inter** for optimal rendering at low display resolutions within Discord's mobile webview, echoing the neutral, high-legibility structure of Discord’s proprietary `gg sans`.

### Hierarchy & Usage
- **Headlines (`headline-lg`, `headline-md`):** Reserved for Modal Headers, Trainer Nicknames, and section titles ("Party Roster", "Base Stats").
- **Body Styles (`body-lg`, `body-md`, `body-sm`):** Handles ability descriptions, moveset details, lore text, and Discord server activity context.
- **Labels & Badges (`label-lg`, `label-md`, `label-sm`):** Rendered in uppercase with slight tracking (`0.02em` - `0.06em`) for elemental type tags, EV/IV numbers, level callouts (`LV. 100`), and stat abbreviations (`HP`, `ATK`, `DEF`, `SPA`, `SPD`, `SPE`).

## Layout & Spacing

Designed specifically for the constrained viewport of mobile Discord Activities, prioritizing vertical flow with rapid finger-reach zones.

### Shell Architecture
- **Header:** Sticky `48px` Discord Activity top bar containing the channel/activity indicator, breadcrumbs, and a right-aligned dismiss ('X') action.
- **Sub-Header:** Server context banner showing trainer avatar, friend code, and competitive tier badge.
- **Body:** Single-column scroll container utilizing a responsive 4-column sub-grid for stats grids and 2-column or 1-column cards for the 6-member party team.
- **Bottom Navigation:** Fixed `56px` bottom tab bar (or top-mounted segmented control if virtual keyboard is active) with high-contrast icon-label pairings for "Party", "Box", "Trainer", and "Rankings".

### Responsive Adaptation
- **Mobile (< 480px):** 1-column stacked party cards (`100%` width), compact stat meters, single-line elemental pill badges. Outer margins fixed at `1rem` (`16px`).
- **Tablet / Discord Desktop Embedded (> 480px):** 2-column party grid, expanded dual-type chips, full EV/IV stat radar integration. Outer margins expand to `1.5rem`.

## Elevation & Depth

Visual hierarchy uses Discord's layered dark surfaces with subtle ambient elevation to separate nested interactions without harsh shadows.

- **Base Layer (`#1E1F22`):** Ground floor; activity canvas underlay.
- **Surface Level 1 (`#232428`):** Main frame containers, tab backgrounds, and unselected inputs.
- **Surface Level 2 (`#2B2D31`):** Default Pokémon cards, trainer info modules, modal dialogs.
- **Surface Level 3 / Hover (`#313338`):** Raised action tiles, active inputs, and elevated card hovers.
- **Borders & Separators:** Thin `1px` borders in `rgba(255, 255, 255, 0.06)` or `#1E1F22` inset separators provide crisp structural separation without heavy visual weight.
- **Drop Shadows:** Kept minimal for mobile performance. Floating modals and active bottom sheets employ `0 8px 24px rgba(0, 0, 0, 0.45)`.

## Shapes

The design uses balanced, modern rounded geometry:
- **Cards & Stat Panels:** `12px` to `16px` border-radius (`rounded-lg`), delivering friendly yet structural framing matching modern Discord modals.
- **Pills & Status Badges:** `8px` (`rounded`) for elemental type tags, status effects (`BRN`, `PAR`, `SLP`), and Level indicators.
- **Controls & Buttons:** `8px` for action buttons and text fields; fully circular (`9999px`) for Pokémon item sprites and circular avatar frames.

## Components

### Top Navigation & Modal Shell
- **Top Bar:** Fixed `48px` height with a dark background (`#1E1F22`). Displays the Discord Activity title with a small server icon on the left, and a standardized close circular button (`28px` with SVG 'X') on the right.
- **Trainer Card Banner:** Sleek user card showing Discord avatar with an integrated level ring, username (`#DBDEE1`), and party win/loss ratio pill.

### Party Cards (The 6-Slot Roster)
- **Container:** `#2B2D31` surface, `12px` radius, subtle `1px` border (`rgba(255, 255, 255, 0.05)`).
- **Layout:** Left section holds the Pokémon animated/static sprite (`56x56px`) with held item icon overlayed on bottom-right. Center section displays species name, gender icon, level badge, and elemental type pills. Right section displays dynamic HP bar, numerical current/max HP, and status ailment chip if applicable.
- **EXP Progress Track:** Slim `3px` track along the bottom edge of each card, filled with Warning Gold (`#F0B232`).

### Elemental Type Chips
- Height: `20px`, padding: `0 8px`, border-radius: `8px`.
- High-contrast bold typography (`label-sm`), uppercase with bold center alignment.

### Stat Gauges (HP, ATK, DEF, SPA, SPD, SPE)
- Two-column or compact single-row stat rows.
- Three-letter label in `#949BA4`, followed by numerical stat value (`#DBDEE1`), and a segmented rounded progress bar with contextual color (Green for high, Yellow for medium, Red for critical/low).

### Buttons & Segmented Controls
- **Primary CTA:** Filled `#5865F2` (Discord Blurple) with white text, `8px` radius, scale active press `0.98`.
- **Secondary CTA:** Surface `#313338` with text `#DBDEE1`.
- **Segmented Control:** Enclosed within `#1E1F22` pill container; active tab slides with a `#313338` background and crisp `#DBDEE1` text.

### Bottom Navigation Bar
- Height: `56px` with safe area inset at base. Surface `#1E1F22` with a `1px` top border (`rgba(255, 255, 255, 0.06)`).
- 4 icons with label text underneath: Party, Box Storage, Battle Stats, Settings. Active tab highlighted in `#5865F2`.