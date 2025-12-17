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
        docker-compose -f docker-compose.dev.yml up --build
        ```
    *   `--build` ser till att bygga om Docker-avbilderna om koden har ändrats.
    *   Om du vill köra tjänsterna i bakgrunden kan du lägga till flaggan `-d`:
        ```bash
        docker-compose -f docker-compose.dev.yml up -d --build
        ```

5.  **Verifiera Installationen**
    Efter att kommandot har körts klart bör alla tjänster vara igång. Du kan nu komma åt de olika delarna av systemet via din webbläsare:
    *   **Incheckningssida:** [http://localhost](http://localhost)
        *   Detta är den publika sidan som deltagare använder. Den dirigeras av Nginx till `backend`-tjänsten.
    *   **Admin Dashboard:** [http://localhost/admin/](http://localhost/admin/)
        *   Detta är TOs instrumentpanel. Den dirigeras av Nginx till `fgt_dashboard`-tjänsten.
    *   **N8N Gränssnitt:** [http://localhost:5678](http://localhost:5678)
        *   Här kan du se och redigera dina n8n-workflows. Du kommer att behöva logga in med användarnamnet och lösenordet du angav i `n8n.env`.

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
