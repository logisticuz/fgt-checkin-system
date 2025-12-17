/**
 * Input validation and sanitization for check-in forms.
 * Mirrors backend validation.py logic.
 */

const Validation = {
  // Field length limits
  MAX_LENGTHS: {
    name: 100,
    phone: 20,
    personnummer: 12,
    personal_id: 12,
    tag: 30,
    nick: 30,
    email: 254,
    discord: 50,
  },

  /**
   * Trim and enforce max length
   */
  sanitizeString(value, fieldName = "") {
    if (typeof value !== "string") return value;

    let result = value.trim();

    const maxLen = this.MAX_LENGTHS[fieldName.toLowerCase()];
    if (maxLen && result.length > maxLen) {
      console.warn(`Field '${fieldName}' truncated from ${result.length} to ${maxLen} chars`);
      result = result.substring(0, maxLen);
    }

    return result;
  },

  /**
   * Normalize phone number to digits only
   * "070-123 45 67" → "0701234567"
   */
  sanitizePhone(value) {
    if (typeof value !== "string") return value;
    return value.replace(/\D/g, "");
  },

  /**
   * Normalize Swedish personal ID number
   * "19900101-1234" → "199001011234"
   */
  sanitizePersonnummer(value) {
    if (typeof value !== "string") return value;
    return value.replace(/[-\s]/g, "").replace(/\D/g, "");
  },

  /**
   * Validate a check-in form and return errors
   */
  validateForm(data) {
    const errors = [];

    // Name required
    const name = data.namn || data.name || "";
    if (!name.trim()) {
      errors.push("Name is required");
    } else if (name.trim().length > this.MAX_LENGTHS.name) {
      errors.push(`Name can be max ${this.MAX_LENGTHS.name} characters`);
    }

    // Personnummer format (if provided)
    const pnr = data.personnummer || data.personal_id || "";
    if (pnr) {
      const cleaned = this.sanitizePersonnummer(pnr);
      if (cleaned.length > 0 && cleaned.length !== 10 && cleaned.length !== 12) {
        errors.push(`Personal ID must be 10 or 12 digits (got ${cleaned.length})`);
      }
    }

    // Phone format (if provided)
    const phone = data.telefon || data.phone || "";
    if (phone) {
      const cleaned = this.sanitizePhone(phone);
      if (cleaned.length > 0 && cleaned.length < 7) {
        errors.push("Phone number too short (minimum 7 digits)");
      }
    }

    // Tag length
    const tag = data.tag || data.nick || "";
    if (tag && tag.trim().length > this.MAX_LENGTHS.tag) {
      errors.push(`Tag can be max ${this.MAX_LENGTHS.tag} characters`);
    }

    return errors;
  },

  /**
   * Sanitize all fields in a form data object
   */
  sanitizeFormData(data) {
    const result = { ...data };

    // String fields (support both Swedish and English field names for backward compat)
    ["namn", "name", "tag", "nick", "email", "discord"].forEach((field) => {
      if (result[field]) {
        result[field] = this.sanitizeString(result[field], field);
      }
    });

    // Phone fields
    ["telefon", "phone"].forEach((field) => {
      if (result[field]) {
        result[field] = this.sanitizePhone(result[field]);
      }
    });

    // Personnummer fields
    ["personnummer", "personal_id"].forEach((field) => {
      if (result[field]) {
        result[field] = this.sanitizePersonnummer(result[field]);
      }
    });

    return result;
  },

  /**
   * Show validation errors to user
   */
  showErrors(errors, containerId = "validation-errors") {
    let container = document.getElementById(containerId);

    // Create container if it doesn't exist
    if (!container) {
      container = document.createElement("div");
      container.id = containerId;
      container.style.cssText = "color: #ff4444; margin: 1rem 0; padding: 0.5rem; border: 1px solid #ff4444; border-radius: 5px;";
      const form = document.querySelector("form");
      if (form) {
        form.insertBefore(container, form.firstChild);
      }
    }

    if (errors.length === 0) {
      container.style.display = "none";
      container.innerHTML = "";
    } else {
      container.style.display = "block";
      container.innerHTML = "<strong>Please correct the following:</strong><ul>" +
        errors.map((e) => `<li>${e}</li>`).join("") +
        "</ul>";
    }
  },

  /**
   * Clear validation errors
   */
  clearErrors(containerId = "validation-errors") {
    const container = document.getElementById(containerId);
    if (container) {
      container.style.display = "none";
      container.innerHTML = "";
    }
  },
};
