package com.github.zaegan.passwordgen;

import java.security.SecureRandom;
import java.util.Arrays;

/**
 * Cryptographically secure password generator.
 *
 * Security notes:
 *  - Uses SecureRandom backed by the OS CSPRNG — never java.util.Random.
 *  - Returns char[] so callers can zero it after use; never returns String
 *    from this class to encourage prompt zeroing at the call site.
 *  - No static state is retained between calls (a fresh SecureRandom instance
 *    is created per call so no seed state lingers).
 *  - Guarantees at least one character from each enabled category before
 *    filling the remainder, then Fisher-Yates shuffles the result so the
 *    guaranteed characters are not always in fixed positions.
 *  - All intermediate char[] buffers are zeroed before the method returns.
 */
public final class PasswordGenerator {

    // ---------- character sets ----------

    private static final String LOWERCASE = "abcdefghijklmnopqrstuvwxyz";
    private static final String UPPERCASE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
    private static final String DIGITS    = "0123456789";
    /** Symbols that are broadly safe in web forms, shells, and password fields. */
    private static final String SYMBOLS   = "!@#$%^&*-_=+?";
    /** Characters commonly mistaken for one another. */
    private static final String AMBIGUOUS = "0Oo1lI";

    private PasswordGenerator() {}

    // ---------- public API ----------

    public static final class Options {
        public boolean includeLower     = true;
        public boolean includeUpper     = true;
        public boolean includeDigits    = true;
        public boolean includeSymbols   = true;
        public boolean excludeAmbiguous = false;
        public int     length           = 16;
    }

    /**
     * Generate a password according to {@code opts}.
     *
     * @return a fresh char[] of length opts.length, or null if no character
     *         type is enabled (or all characters are removed by the ambiguous
     *         filter). Caller MUST zero the array with Arrays.fill(result, '\0')
     *         as soon as the value is no longer needed.
     */
    public static char[] generate(Options opts) {
        // Build the full pool from enabled sets (pre-filter)
        String lower   = opts.includeLower   ? filter(LOWERCASE, opts.excludeAmbiguous) : "";
        String upper   = opts.includeUpper   ? filter(UPPERCASE, opts.excludeAmbiguous) : "";
        String digits  = opts.includeDigits  ? filter(DIGITS,    opts.excludeAmbiguous) : "";
        String symbols = opts.includeSymbols ? SYMBOLS : "";   // SYMBOLS has no ambiguous chars

        String fullPool = lower + upper + digits + symbols;
        if (fullPool.isEmpty()) return null;

        char[] pool    = fullPool.toCharArray();
        char[] result  = new char[opts.length];
        SecureRandom   rng = new SecureRandom();

        try {
            // Phase 1: guarantee at least one char from each enabled (non-empty) set.
            // Place them at the START of result[] — they'll be shuffled later.
            int guaranteed = 0;
            if (!lower.isEmpty()   && guaranteed < opts.length)
                result[guaranteed++] = lower.charAt(rng.nextInt(lower.length()));
            if (!upper.isEmpty()   && guaranteed < opts.length)
                result[guaranteed++] = upper.charAt(rng.nextInt(upper.length()));
            if (!digits.isEmpty()  && guaranteed < opts.length)
                result[guaranteed++] = digits.charAt(rng.nextInt(digits.length()));
            if (!symbols.isEmpty() && guaranteed < opts.length)
                result[guaranteed++] = symbols.charAt(rng.nextInt(symbols.length()));

            // Phase 2: fill the rest from the full pool.
            for (int i = guaranteed; i < opts.length; i++) {
                result[i] = pool[rng.nextInt(pool.length)];
            }

            // Phase 3: Fisher-Yates shuffle — guaranteed chars no longer in fixed positions.
            for (int i = opts.length - 1; i > 0; i--) {
                int j = rng.nextInt(i + 1);
                char tmp  = result[i];
                result[i] = result[j];
                result[j] = tmp;
            }

            return result;

        } finally {
            // Zero intermediate data. result[] is the caller's responsibility.
            Arrays.fill(pool, '\0');
        }
    }

    // ---------- helpers ----------

    private static String filter(String src, boolean removeAmbiguous) {
        if (!removeAmbiguous) return src;
        StringBuilder sb = new StringBuilder(src.length());
        for (int i = 0; i < src.length(); i++) {
            char c = src.charAt(i);
            if (AMBIGUOUS.indexOf(c) < 0) sb.append(c);
        }
        return sb.toString();
    }
}
