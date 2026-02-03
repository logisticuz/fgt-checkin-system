# 0. Problem och Lösning

## Problemet: Manuell och tidskrävande incheckning

Att arrangera ett spelevent eller en turnering innebär många logistiska utmaningar. En av de mest tidskrävande och felbenägna processerna är incheckningen av deltagare. Turneringsorganisatörer (TOs) måste manuellt verifiera flera saker för varje enskild deltagare:

1.  **Medlemskap i föreningen:** Är deltagaren en betalande medlem i den arrangerande föreningen (t.ex. via Sverok)? Detta kräver ofta slagningar i separata medlemsregister.
2.  **Turneringsregistrering:** Är deltagaren korrekt registrerad för rätt spel i den aktuella turneringen på en plattform som Start.gg?
3.  **Betalningsstatus:** Har deltagaren betalat eventavgiften? Detta innebär ofta att manuellt behöva stämma av Swish-betalningar mot en lista med deltagare.

Denna manuella process leder till:
*   **Långa köer:** Deltagare får vänta länge på sin tur att checka in.
*   **Stress för arrangörer:** TOs måste hantera flera system samtidigt under tidspress.
*   **Mänskliga fel:** Det är lätt att missa en betalning, en registrering eller att felaktigt godkänna en deltagare.
*   **Brist på realtidsdata:** Det är svårt att få en omedelbar överblick över hur många som är fullt incheckade och redo att spela.

## Lösningen: Ett Automatiserat Incheckningssystem

Detta verktyg är byggt för att lösa dessa problem genom att automatisera hela incheckningsflödet. Systemet erbjuder en centraliserad och smidig lösning för både deltagare och arrangörer.

### Hur det fungerar

1.  **Självbetjäning för deltagare:** Deltagaren möts av en enkel webbsida där de fyller i sina grundläggande uppgifter (namn, gamer-tag, etc.) för att checka in.
2.  **Automatisk verifiering:** I bakgrunden anropar systemet automatiskt de nödvändiga externa tjänsterna:
    *   **Sverok eBas API** för att verifiera medlemskap.
    *   **Start.gg API** för att verifiera turneringsregistrering.
    *   Kontrollerar betalningsstatus mot data i en central databas (Airtable).
3.  **Omedelbar feedback:**
    *   Om allt är i sin ordning får deltagaren en "Grön" status och är redo att spela.
    *   Om något saknas (t.ex. medlemskap eller turneringsregistrering), presenteras ett dynamiskt formulär som guidar deltagaren att slutföra de saknade stegen direkt på plats.
4.  **Realtids-instrumentpanel för TOs:** Arrangörerna har tillgång till en administrativ instrumentpanel (`dashboard`) som i realtid visar status för alla deltagare. De kan omedelbart se vilka som är redo, vilka som väntar på något, och vilka som behöver hjälp.

### Mål och fördelar

*   **Snabbare incheckning:** Minskar väntetiden för deltagare från minuter till sekunder.
*   **Mindre manuellt arbete:** Frigör tid och minskar stressen för turneringsorganisatörer.
*   **Högre datakvalitet:** Eliminerar risken för mänskliga fel och säkerställer att alla kontroller har utförts korrekt.
*   **Bättre överblick:** Ger TOs en live-uppdaterad överblick över incheckningsprocessen, vilket underlättar planeringen av turneringen.
*   **Smidigare upplevelse:** Skapar en modern och professionell upplevelse för alla inblandade.
