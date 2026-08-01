import requests
import smtplib
from email.message import EmailMessage
import os

EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

URL = "https://www.keychron.com/products/keychron-k5-ultra-8k-wireless-custom-mechanical-keyboard"

WORDS = [
    "Nordic",
    "Danish",
    "ISO Nordic",
    "ANSI",
    "ISO"
]


def check_keychron():

    response = requests.get(URL, timeout=20)

    response.raise_for_status()

    page = response.text.lower()

    for word in WORDS:
        if word in page:
            return True, word

    return False, None



def send_mail(found_word):

    msg = EmailMessage()

    msg["Subject"] = "Keychron K5 Ultra Nordic fundet!"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    msg.set_content(
        f"""
Keychron K5 Ultra ser ud til at have fået dansk/nordisk layout.

Fundet ord:
{found_word}

Link:
{URL}
"""
    )


    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465
    ) as smtp:

        smtp.login(
            EMAIL_FROM,
            EMAIL_PASSWORD
        )

        smtp.send_message(msg)



if __name__ == "__main__":

    found, word = check_keychron()

    print("Result:", found, word)

    if found:
        send_mail(word)
