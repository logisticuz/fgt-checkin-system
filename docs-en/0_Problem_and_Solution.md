# 0. Problem and Solution

## The Problem: Manual and Time-Consuming Check-ins

Organizing a gaming event or tournament involves many logistical challenges. One of the most time-consuming and error-prone processes is checking in participants. Tournament Organizers (TOs) must manually verify several things for each individual participant:

1.  **Association Membership:** Is the participant a paying member of the organizing association (e.g., via Sverok)? This often requires looking up information in separate member registries.
2.  **Tournament Registration:** Is the participant correctly registered for the right game in the current tournament on a platform like Start.gg?
3.  **Payment Status:** Has the participant paid the event fee? This often means manually cross-referencing Swish payments with a list of participants.

This manual process leads to:
*   **Long Queues:** Participants have to wait a long time for their turn to check in.
*   **Stress for Organizers:** TOs have to manage multiple systems simultaneously under time pressure.
*   **Human Error:** It's easy to miss a payment, a registration, or to incorrectly approve a participant.
*   **Lack of Real-time Data:** It is difficult to get an immediate overview of how many people are fully checked in and ready to play.

## The Solution: An Automated Check-in System

This tool is built to solve these problems by automating the entire check-in flow. The system offers a centralized and smooth solution for both participants and organizers.

### How It Works

1.  **Self-Service for Participants:** The participant is greeted by a simple webpage where they fill in their basic information (name, tag, etc.) to check in.
2.  **Automatic Verification:** In the background, the system automatically calls the necessary external services:
    *   **Sverok eBas API** to verify membership.
    *   **Start.gg API** to verify tournament registration.
    *   Checks payment status against data in a central database (Airtable).
3.  **Immediate Feedback:**
    *   If everything is in order, the participant gets a "Green" status and is ready to play.
    *   If something is missing (e.g., membership or tournament registration), a dynamic form is presented that guides the participant to complete the missing steps on the spot.
4.  **Real-time Dashboard for TOs:** Organizers have access to an administrative dashboard that displays the status of all participants in real time. They can immediately see who is ready, who is waiting for something, and who needs help.

### Goals and Benefits

*   **Faster Check-ins:** Reduces the waiting time for participants from minutes to seconds.
*   **Less Manual Work:** Frees up time and reduces stress for tournament organizers.
*   **Higher Data Quality:** Eliminates the risk of human error and ensures that all checks have been performed correctly.
*   **Better Overview:** Gives TOs a live-updated overview of the check-in process, which facilitates tournament planning.
*   **Smoother Experience:** Creates a modern and professional experience for everyone involved.
