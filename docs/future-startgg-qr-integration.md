# Future: Start.gg QR Code Integration

**Status:** Parkerad - diskuteras senare

---

## Start.gg's QR-funktioner

1. **Admin Modal** - Öppnar info om spelaren (för TOs)
2. **Custom URL med `{token}`** - Skickar till egen URL
3. **Auto check-in** - Markerar dem som incheckade på Start.gg

---

## Potentiell integration

### Custom URL approach

```
https://checkin.fgctrollhattan.se/qr/{token}
```

**Flöde:**
1. Spelare scannar QR-kod (från Start.gg mail/app)
2. Hamnar på vår check-in med token
3. Backend slår upp token → hämtar namn/tag automatiskt
4. Spelaren slipper skriva något - bara bekräfta

**Fördelar:**
- Snabbare check-in (ingen manuell inmatning)
- Garanterat att de är reggade på Start.gg (har token)
- Behåller egen verifiering (betalning, medlemskap)

**Nackdel:**
- Fungerar bara för de som reggat sig på Start.gg i förväg
- "Guests" måste fortfarande använda vanliga formuläret

### Start.gg Auto check-in approach

Start.gg API har `isCheckedIn` fält på participants.

**Flöde:**
1. Spelare scannar Start.gg QR → Start.gg markerar dem incheckade
2. Spelaren kommer till vårt vanliga check-in formulär
3. Vår backend ser att `isCheckedIn = true` på Start.gg
4. → Automatiskt ✅ på Start.gg-kravet

**Men** - vi behöver fortfarande verifiera betalning/medlemskap och skapa post i Airtable.

---

## Öppna frågor

- Vad är `{token}` egentligen? Participant ID?
- Kan vi slå upp en spelare via token i Start.gg API?
- Värt komplexiteten vs nuvarande manuella flöde?

---

*Skapad: 2026-01-28*
