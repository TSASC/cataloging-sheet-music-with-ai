from imslp_estimator.core import estimate_year


def main():
    print("\n📘 IMSLP Publication Date Estimator")

    publisher_input = input("Enter publisher name: ").strip()
    address_input = input("Enter publisher address (if none, press Enter): ").strip()
    plate_input = input("Enter plate number (if none, press Enter): ").strip()

    result, error = estimate_year(publisher_input, plate_input, address_input)

    print("\n📄 Result:")
    if error:
        print("❌", error)
        return

    publisher = result["publisher"]
    link = result["link"] or "❌ No page link"
    plate = result["plate"] or "-"
    address = result["address"] or "-"
    city = result["city"] or "-"
    print(f"Publisher: {publisher} ✅ Found")
    print(f"🔗 IMSLP Page: {link}")
    print(f"Plate Number: {plate}")
    print(f"Address: {address}")
    print(f"City: {city}")

    if result["estimated_year"]:
        symbol = "🔵" if "wiki" in result["source"] else "🟡"
        print(
            f"🗓️ Estimated Year: {result['estimated_year']}  ({symbol} Estimated {result['source']})"
        )
    else:
        print(
            "❌ No estimated year found. Publisher link may not contain relevant info."
        )


if __name__ == "__main__":
    main()
