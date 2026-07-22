/* Country phone data — code, flag emoji, name, max digits (excluding code), example.
   Sorted by commonality. */
window.COUNTRIES = [
  { code: "+1",   flag: "🇺🇸", name: "US",        maxDigits: 10, pattern: /^\d{10}$/, example: "(555) 123-4567" },
  { code: "+1",   flag: "🇨🇦", name: "Canada",    maxDigits: 10, pattern: /^\d{10}$/, example: "416 555 0198" },
  { code: "+44",  flag: "🇬🇧", name: "UK",        maxDigits: 10, pattern: /^\d{10}$/, example: "7911 123456" },
  { code: "+61",  flag: "🇦🇺", name: "Australia", maxDigits: 9,  pattern: /^\d{9}$/,  example: "412 345 678" },
  { code: "+49",  flag: "🇩🇪", name: "Germany",   maxDigits: 10, pattern: /^\d{10,11}$/, example: "151 1234567" },
  { code: "+33",  flag: "🇫🇷", name: "France",    maxDigits: 9,  pattern: /^\d{9}$/,  example: "6 12 34 56 78" },
  { code: "+39",  flag: "🇮🇹", name: "Italy",     maxDigits: 10, pattern: /^\d{10}$/, example: "312 345 6789" },
  { code: "+34",  flag: "🇪🇸", name: "Spain",     maxDigits: 9,  pattern: /^\d{9}$/,  example: "612 34 56 78" },
  { code: "+55",  flag: "🇧🇷", name: "Brazil",    maxDigits: 10, pattern: /^\d{10,11}$/, example: "11 91234-5678" },
  { code: "+52",  flag: "🇲🇽", name: "Mexico",    maxDigits: 10, pattern: /^\d{10}$/, example: "55 1234 5678" },
  { code: "+81",  flag: "🇯🇵", name: "Japan",     maxDigits: 10, pattern: /^\d{10,11}$/, example: "90 1234 5678" },
  { code: "+82",  flag: "🇰🇷", name: "Korea",     maxDigits: 10, pattern: /^\d{10}$/, example: "10 1234 5678" },
  { code: "+86",  flag: "🇨🇳", name: "China",     maxDigits: 11, pattern: /^\d{11}$/, example: "138 0013 8000" },
  { code: "+91",  flag: "🇮🇳", name: "India",     maxDigits: 10, pattern: /^\d{10}$/, example: "98765 43210" },
  { code: "+7",   flag: "🇷🇺", name: "Russia",    maxDigits: 10, pattern: /^\d{10}$/, example: "912 345 67 89" },
  { code: "+971", flag: "🇦🇪", name: "UAE",       maxDigits: 9,  pattern: /^\d{9}$/,  example: "50 123 4567" },
  { code: "+966", flag: "🇸🇦", name: "Saudi Arabia", maxDigits: 9, pattern: /^\d{9}$/, example: "55 123 4567" },
  { code: "+92",  flag: "🇵🇰", name: "Pakistan",  maxDigits: 10, pattern: /^\d{10}$/, example: "300 1234567" },
  { code: "+880", flag: "🇧🇩", name: "Bangladesh", maxDigits: 10, pattern: /^\d{10}$/, example: "1712 345678" },
  { code: "+62",  flag: "🇮🇩", name: "Indonesia", maxDigits: 10, pattern: /^\d{10,11}$/, example: "812 3456 7890" },
  { code: "+63",  flag: "🇵🇭", name: "Philippines", maxDigits: 10, pattern: /^\d{10}$/, example: "912 345 6789" },
  { code: "+84",  flag: "🇻🇳", name: "Vietnam",   maxDigits: 9,  pattern: /^\d{9,10}$/, example: "91 234 56 78" },
  { code: "+66",  flag: "🇹🇭", name: "Thailand",  maxDigits: 9,  pattern: /^\d{9}$/,  example: "81 234 5678" },
  { code: "+234", flag: "🇳🇬", name: "Nigeria",   maxDigits: 10, pattern: /^\d{10}$/, example: "803 123 4567" },
  { code: "+27",  flag: "🇿🇦", name: "South Africa", maxDigits: 9, pattern: /^\d{9}$/, example: "82 123 4567" },
  { code: "+254", flag: "🇰🇪", name: "Kenya",     maxDigits: 9,  pattern: /^\d{9}$/,  example: "712 345 678" },
  { code: "+233", flag: "🇬🇭", name: "Ghana",     maxDigits: 9,  pattern: /^\d{9}$/,  example: "54 123 4567" },
  { code: "+20",  flag: "🇪🇬", name: "Egypt",     maxDigits: 10, pattern: /^\d{10}$/, example: "10 0123 4567" },
  { code: "+972", flag: "🇮🇱", name: "Israel",    maxDigits: 9,  pattern: /^\d{9}$/,  example: "50 123 4567" },
  { code: "+90",  flag: "🇹🇷", name: "Turkey",    maxDigits: 10, pattern: /^\d{10}$/, example: "532 123 45 67" },
  { code: "+31",  flag: "🇳🇱", name: "Netherlands", maxDigits: 9, pattern: /^\d{9}$/, example: "6 12 345 678" },
  { code: "+46",  flag: "🇸🇪", name: "Sweden",    maxDigits: 9,  pattern: /^\d{9}$/,  example: "70 123 45 67" },
  { code: "+47",  flag: "🇳🇴", name: "Norway",    maxDigits: 8,  pattern: /^\d{8}$/,  example: "412 34 567" },
  { code: "+45",  flag: "🇩🇰", name: "Denmark",   maxDigits: 8,  pattern: /^\d{8}$/,  example: "20 12 34 56" },
  { code: "+358", flag: "🇫🇮", name: "Finland",   maxDigits: 9,  pattern: /^\d{9}$/,  example: "40 123 4567" },
  { code: "+48",  flag: "🇵🇱", name: "Poland",    maxDigits: 9,  pattern: /^\d{9}$/,  example: "600 123 456" },
  { code: "+351", flag: "🇵🇹", name: "Portugal",  maxDigits: 9,  pattern: /^\d{9}$/,  example: "912 345 678" },
  { code: "+30",  flag: "🇬🇷", name: "Greece",    maxDigits: 10, pattern: /^\d{10}$/, example: "691 234 5678" },
  { code: "+353", flag: "🇮🇪", name: "Ireland",   maxDigits: 9,  pattern: /^\d{9}$/,  example: "85 123 4567" },
  { code: "+64",  flag: "🇳🇿", name: "New Zealand", maxDigits: 9, pattern: /^\d{9}$/, example: "21 123 4567" },
  { code: "+65",  flag: "🇸🇬", name: "Singapore", maxDigits: 8,  pattern: /^\d{8}$/,  example: "9123 4567" },
  { code: "+60",  flag: "🇲🇾", name: "Malaysia",  maxDigits: 9,  pattern: /^\d{9,10}$/, example: "12 345 6789" },
  { code: "+852", flag: "🇭🇰", name: "Hong Kong", maxDigits: 8,  pattern: /^\d{8}$/,  example: "5123 4567" },
  { code: "+886", flag: "🇹🇼", name: "Taiwan",    maxDigits: 9,  pattern: /^\d{9}$/,  example: "912 345 678" },
];

/* Build a lookup map keyed by country code (first match wins). */
window.COUNTRY_MAP = {};
for (const c of window.COUNTRIES) {
  if (!window.COUNTRY_MAP[c.code]) window.COUNTRY_MAP[c.code] = c;
}

/* Return the country entry for a full phone number (e.g. "+919876543210"). */
function detectCountry(phone) {
  if (!phone || !phone.startsWith("+")) return null;
  // Try longest code first (3-digit codes before 2-digit)
  for (const c of window.COUNTRIES) {
    if (phone.startsWith(c.code)) return c;
  }
  return null;
}

/* Get max local digits for a country code. */
function maxLocalDigits(code) {
  const c = window.COUNTRY_MAP[code];
  return c ? c.maxDigits : 15;
}
