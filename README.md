# cataloging-sheet-music-with-ai
A process developed for our Advancing Open Knowledge grant, using a python script to extract cataloging metadata obtained from OCR-ed title page scans in ChatGPT, into a .csv, for ingestion into finding aids. 
# Disclaimer
In no respect shall TSASC incur any liability for any damages, including, but limited to, direct, indirect, special, or consequential damages arising out of, resulting from, or any way connected to the use of the item, whether or not based upon warranty, contract, tort, or otherwise; whether or not injury was sustained by persons or property or otherwise; and whether or not loss was sustained from, or arose out of, the results of, the item, or any services that may be provided by TSASC.
# Getting started
This code is created on the assumption that users will provide ONE scan per transcription/transliteration result. We scan the score title pages ourselves, then if we have the $$, we send the scans we used to the DRS to be linked to the ArchivesSpace object.\
You will need:\
•	Scans displaying identifiable metadata\
•	A ChatGPT API key. Contact HUIT (or your IT division) to arrange this\
•	Access to a Google drive, to upload this Colab python script, and to access scans to run through the code
# Prepare your prompt
Effective prompts require three parts:\
•	Start with a description of the overall context\
•	Provide the steps of the request, detail by detail, preferably in a specified order\
•	Specify your output

•	NB: as you craft a functional prompt, keep track of your iterations, with notes. (We used a Google doc, with a table of contents.)

Here is a sample generic prompt, to give you place to start:

("You are a cataloger creating structured data from an image."\
"You will need to manually transcribe the data from the images."\
"You will extract the information from the image and present it in three fields: title, statement_of_responsibility, and publication_info."\
"Do not number each extract."\
"Do not stop to ask whether I want to add anything to a transcription, simply proceed with the next set of transcriptions until all uploaded scans have been completed."\
"Please don't use markdown to present the result."\
"Use ISBD punctuation and RDA (Resource Description & Access) capitalization rules."\
"Initials and acronyms are to be transcribed without internal spaces, regardless of how they are presented on the image. For example, 'F.K.E. Kennedy', NOT 'F. K. E. Kennedy'."\
"Your response should only contain the data requested. No additional explanation and no extra information. Only transcribe information which is directly stated in the scan. Do not add any outside information, speculation, or generative text. Stick strictly to the text presented in the scan. If you cannot read, or cannot correctly transcribe something, you may return a result of '[illegible]' particularly for text in very small fonts."\
"First extract the title. The title field should be COMPLETE, including subtitles. If the image has subtitles, please put a ' : ' between them and the title, such as 'Andante et mazurka : pour flûte avec accompagnement de piano'."\
"Capitalization: only the first letter of the first word of each title or subtitle should be a capital even when the title has been changed from all caps, and the first letter of the first word of parallel titles, alternative titles, or section titles should also be a capital, again, even when the title has been changed from all caps. Other capitalization should follow the appropriate usage for the language used in the title. For instance, in German, be sure to capitalize nouns."\
"Second, extract the statement_of_responsibility field, and you should also transcribe words like 'by', 'par', 'von', etc. exactly as found in the image."\
"If you transcribe more than one statement of responsibility, please add a ' ; ' between them."\
"Third, extract publication_info field, which should be in the format of 'Place of publication : publisher, date' and should include only ONE publisher, the most predominant one."\
"If there is any kind of statement on the image with the word 'edition' or 'ed.' in it, in any language, please include this statement in the 'publication_info' before the 'Place of publication : publisher, date'. Please look carefully for these edition statements, as they can be found at the head of the title, anywhere within the main title information, or even at the foot of the page."\
"If place of publication, or publisher, are not found, DO NOT SUPPLY."\
"Finally, please format the output like this: ǂt 'title' / ǂr 'statement_of_responsibility'. ǂg 'publication_info' -- "\
"Here are some examples with the correct format:\
ǂt Phantasie : für das Piano Forte ... Op. 28 / ǂr componirt von Felix Mendelssohn Bartholdy. ǂg Bonn : H. Simrock, [1834?] \
ǂt Le réveil des fées : étude pour piano ... op. 41 / ǂr par Émile Prudent. ǂg Bruxelles : Schott frères, [no date]\
ǂt Fort Mitchell polka / ǂr composed and arranged by Francis Rziha (leader of the Steyermarkische Co.). ǂg Philadelphia : E. Ferrett & Co., ©1849 -- ")

NB: The output keys you choose must match exactly the fields specified in the Python code, including the case!!

General things to keep in mind:\
●	AI works reasonably well for simple transcription and transliteration from scans but must be proofread, particularly as you are developing your prompt\
●	ChatGPT can identify titles, subtitles, statements of responsibility, and imprint metadata, most of the time, within the Harvard ChatGPT system\
●	AI works much better with letters than numbers:\
  ○	Dates and plate numbers must be carefully proofread\
●	Keep prompt demands simple!\
  ○	AI isn’t ready for complex catalog formatting (correct series title transcription remains a distant dream)\
●	Analysis-based tasks like classing or assigning subject headings may be possible in future, but not with current online resources for reference

# Documentation
This documentation should guide you through the idiosyncracies of using this Colab code. You can use an overhead scanner like we did, or you can even use a phone camera (though we don’t recommend this: lots of wear and tear on your phone). However you plan to generate the scans which will be run through the program, create the highest resolution for the best results.

Scanning:\
•	Create folder for collections in your local Pictures folder (if you cannot scan to a folder in Google Drive):\
•	Set up your scanner, check that all connections and settings are correct.\
•	Browse through your sheet music [or other items]: be aware of WHAT you are going to scan for the best results.\
•	Scan title pages into the local folder (unless you can scan them directly into a Google drive folder)\
•	When finished scanning box/folder, if you weren’t able to scan your images directly into a Google Drive folder, create, then navigate to a storage folder in Google shared drive, and upload your scans into a folder on Google Drive.\
•	FIRST TIME USERS OF COLAB CODE: CREATE A SEPARATE COPY OF THE COLAB FOR YOURSELF: File> Save as a copy in Drive, RENAME (close "release notes" if it opens). We used the naming protocol: ANDREA’S COPY OF … to keep different project and cataloger’s codes distinguished.\
  o	To add API key, installation and setup: KEY icon on left\
  o	ADD NEW SECRET:\
  o	name: OPENAI_API_KEY\
  o	value: [API]\
  o	toggle under NOTEBOOK ACCESS, to enter/activate\
  o	close SECRETS, upper right X\
  o	ONCE YOU HAVE DONE ALL OF THIS, you can use the same "copy of ..." for each run, simply changing the scans address, updating prompt, and any other variables found in the first code cell\
•	Open your Colab code, is the prompt current? If not, check your Prompts documentation, and copy your current prompt or specific collection prompt.\
•	Check the Google Drive scan folder address, make sure it matches the address in the 1st code cell:\
•	Run 1st code cell:\
  o	"Grant access" to secret code, if needed, then run again\
  o	Allow access to Google drive, follow pop up windows\
  o	what to access: SELECT ALL\
  o	(If “drive mount” fails, stop and run 1st code cell again)\
  o	(If any text in prompt turns black, this represents an error: hover over opening parens, see if there is a clue to what is wrong)\
  o	Look for green check upper left, done updating 1st code cell\
•	Move on to 2nd code cell and run\
•	Move to 3rd cell, run. This cell will fail if you have used an incorrect GPT API, or if your scan location is incorrect.\
•	Run 4th cell:\
  o	This will run the scans thru the Python code; you may follow its progress at the bottom of the cell\
  o	When the run is finished, you will see a message something like this, and you will find these two spreadsheets in your Google Drive:\
  	Information extracted and saved to /content/drive/My Drive/output_Lowerre_148.csv\
  	CSV successfully converted to Excel and saved to /content/drive/My Drive/Output_Lowerre_148.xlsx\
  o	If you have made code changes, save your Colab: FILE> Save\
•	NB: running the Colab will overwrite any older output folder in your Google drive of the same name, unless output name or location in code cell 1 is changed\
•	If all goes well, you will then have an output file in your Google Drive. Navigate to your drive, find “output_[foldername].csv”, OPEN WITH Google sheets, and proofread your transcribed metadata.\
•	Save your Google spreadsheet, or convert it to Excel!\
•	Prepare Excel spreadsheet for ingestion into ArchivesSpace, should this be part of your protocol.

It is possible to run this code through a larger program, and factor in other programming. As part of our AOK grant, our student developed a “look-up” to search for publishing dates, for instance. This might be useful for a variety of other cataloging functions which aren’t covered in the Colab code.
