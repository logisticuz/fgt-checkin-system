# 3. Installation och Setup

Denna guide beskriver hur man sätter upp och kör projektet i en lokal utvecklingsmiljö. Hela systemet är container-baserat med Docker, vilket förenklar installationen avsevärt.

---

### Förkrav

*   **Docker:** Du måste ha Docker installerat på din dator. [Ladda ner Docker Desktop](https://www.docker.com/products/docker-desktop/).
*   **Docker Compose:** Detta inkluderas vanligtvis med Docker Desktop.
*   **Git:** För att klona projektets repository.
*   **En textredigerare:** T.ex. Visual Studio Code.

---

### Steg-för-steg-guide

1.  **Klona Projektet**
    Öppna en terminal eller kommandotolk och kör följande kommando för att ladda ner projektfilerna från GitHub:
    ```bash
    git clone <URL-till-ditt-git-repository>
    cd fgt-checkin-system
    ```

2.  **Konfigurera Miljövariabler (`.env`)**
    Systemet är beroende av ett antal "hemligheter" och API-nycklar. Dessa ska **inte** sparas direkt i koden.
    *   Hitta filen `.env.example` i projektets rotmapp.
    *   Skapa en kopia av denna fil och döp den till `.env`.
    *   Öppna den nya `.env`-filen och fyll i alla nödvändiga värden. Detta inkluderar:
        *   `AIRTABLE_API_KEY` och `AIRTABLE_BASE_ID` från ditt Airtable-konto.
        *   `STARTGG_API_KEY` från ditt Start.gg-konto.
        *   Andra relevanta nycklar och konfigurationer.

3.  **Konfigurera N8N (`n8n.env`)**
    Även `n8n`-tjänsten behöver sina egna miljövariabler, särskilt för att sätta upp användarnamn och lösenord.
    *   Navigera till mappen `n8n/config/`.
    *   Hitta filen `n8n.env.example`.
    *   Skapa en kopia och döp den till `n8n.env`.
    *   Öppna den nya `n8n.env`-filen och fyll i `N8N_BASIC_AUTH_USER` och `N8N_BASIC_AUTH_PASSWORD` för att skydda ditt n8n-gränssnitt.

4.  **Bygg och Starta Tjänsterna**
    När konfigurationsfilerna är på plats är det dags att starta systemet. Se till att du står i projektets rotmapp i din terminal.
    *   Kör följande kommando för att bygga och starta alla tjänster i utvecklingsläge:
        ```bash
        docker compose -p fgt-dev -f docker-compose.dev.yml up --build
        ```
    *   `-p fgt-dev` ger dev-stacken ett unikt projektnamn för att undvika konflikter med produktionsstacken.
    *   `--build` ser till att bygga om Docker-avbilderna om koden har ändrats.
    *   Om du vill köra tjänsterna i bakgrunden kan du lägga till flaggan `-d`.

5.  **Verifiera Installationen**
    Efter att kommandot har körts klart bör alla tjänster vara igång. Du kan nu komma åt de olika delarna av systemet via din webbläsare på deras nya dev-portar:
    *   **Incheckningssida & Dashboard:** [http://localhost:8088](http://localhost:8088) och [http://localhost:8088/admin/](http://localhost:8088/admin/)
        *   Detta är den publika sidan och TO-dashboarden, dirigerade via Nginx dev-konfiguration.
    *   **N8N Gränssnitt:** [http://localhost:5679](http://localhost:5679)
        *   Här kan du se och redigera dina n8n-workflows.
    *   **Backend API direkt:** [http://localhost:8001](http://localhost:8001)

### Parallellkörning av Dev & Prod (Avancerat)

Systemet är konfigurerat för att kunna köra utvecklings- och produktionsmiljöerna samtidigt på samma maskin. Detta är användbart för att testa lokalt utan att störa den live-tjänst som är kopplad till domänerna.

*   **Produktionsstacken** använder standardportarna (80, 443) och körs med kommandot:
    ```bash
    docker compose -p fgt-prod -f docker-compose.prod.yml up -d
    ```
*   **Utvecklingsstacken** (som beskrivits ovan) använder alternativa portar (8088, 5679, etc.) och ett separat projektnamn (`-p fgt-dev`).

### Notering om n8n-datavolym

Både `docker-compose.dev.yml` och `docker-compose.prod.yml` är konfigurerade att använda en **delad, extern Docker-volym** vid namn `fgt-checkin-system_n8n_data`. Detta säkerställer att båda miljöerna använder samma n8n-data (workflows, credentials, etc.) och förhindrar att data försvinner när en stack tas ner och upp. Du behöver vanligtvis inte hantera detta manuellt, men det är bra att känna till.

### Felsökning

*   **Se loggar:** Om en tjänst inte startar korrekt kan du se dess loggar genom att köra:
    ```bash
    # Se alla loggar i realtid
    docker-compose -f docker-compose.dev.yml logs -f

    # Se loggen för en specifik tjänst (t.ex. backend)
    docker-compose -f docker-compose.dev.yml logs -f backend
    ```
*   **Stänga av systemet:** För att stoppa alla tjänster, kör:
    ```bash
    docker-compose -f docker-compose.dev.yml down
    ```
